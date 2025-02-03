import random, time, traceback
import os.path as osp
import torch
import torch.nn.functional as F
import numpy as np
import yaml
from typing import List, Tuple, Any

from utils import length_to_mask, maximum_path, log_norm, log_print, get_image
from monotonic_align import mask_from_lens
from losses import magphase_loss

###############################################
# Helper Functions
###############################################


def prepare_batch(
    batch: List[Any], device: torch.device, keys_to_transfer: List[str] = None
) -> Tuple:
    """
    Transfers selected batch elements to the specified device.
    """
    if keys_to_transfer is None:
        keys_to_transfer = [
            "waves",
            "texts",
            "input_lengths",
            "ref_texts",
            "ref_lengths",
            "mels",
            "mel_input_length",
            "ref_mels",
        ]
    index = {
        "waves": 0,
        "texts": 1,
        "input_lengths": 2,
        "ref_texts": 3,
        "ref_lengths": 4,
        "mels": 5,
        "mel_input_length": 6,
        "ref_mels": 7,
    }
    prepared = tuple()
    for key in keys_to_transfer:
        if key not in index:
            raise ValueError(
                f"Key {key} not found in batch; valid keys: {list(index.keys())}"
            )
        prepared += (batch[index[key]].to(device),)
    return prepared


def compute_alignment(
    train,
    mels: torch.Tensor,
    texts: torch.Tensor,
    input_lengths: torch.Tensor,
    mel_input_length: torch.Tensor,
    apply_attention_mask: bool = False,
    use_random_choice: bool = False,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Computes the alignment used for training.
    Returns:
      - s2s_attn: Raw attention from the text aligner.
      - s2s_attn_mono: Monotonic attention path.
      - asr: Encoded representation from the text encoder.
      - text_mask: Mask for text.
      - mask: Mel mask used for the aligner.
    """
    # Create masks.
    mask = length_to_mask(mel_input_length // (2**train.n_down)).to(train.device)
    text_mask = length_to_mask(input_lengths).to(train.device)

    # --- Text Aligner Forward Pass ---
    with train.accelerator.autocast():
        ppgs, s2s_pred, s2s_attn = train.model.text_aligner(mels, mask, texts)
        s2s_attn = s2s_attn.transpose(-1, -2)
        s2s_attn = s2s_attn[..., 1:]
        s2s_attn = s2s_attn.transpose(-1, -2)

    # Optionally apply extra attention mask.
    if apply_attention_mask:
        with torch.no_grad():
            attn_mask = (
                (~mask)
                .unsqueeze(-1)
                .expand(mask.shape[0], mask.shape[1], text_mask.shape[-1])
                .float()
                .transpose(-1, -2)
            )
            attn_mask = (
                attn_mask
                * (~text_mask)
                .unsqueeze(-1)
                .expand(text_mask.shape[0], text_mask.shape[1], mask.shape[-1])
                .float()
            )
            attn_mask = attn_mask < 1
        s2s_attn.masked_fill_(attn_mask, 0.0)

    # --- Monotonic Attention Path ---
    with torch.no_grad():
        mask_ST = mask_from_lens(
            s2s_attn, input_lengths, mel_input_length // (2**train.n_down)
        )
        s2s_attn_mono = maximum_path(s2s_attn, mask_ST)

    # --- Text Encoder Forward Pass ---
    with train.accelerator.autocast():
        t_en = train.model.text_encoder(texts, input_lengths, text_mask)
        if use_random_choice:
            asr = t_en @ (s2s_attn if bool(random.getrandbits(1)) else s2s_attn_mono)
        else:
            asr = t_en @ s2s_attn_mono

    return s2s_attn, s2s_attn_mono, s2s_pred, asr, text_mask, mask


def compute_duration_ce_loss(
    s2s_preds: List[torch.Tensor],
    text_inputs: List[torch.Tensor],
    text_lengths: List[int],
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Computes the duration and binary cross-entropy losses over a batch.
    Returns (loss_ce, loss_dur).
    """
    loss_ce = 0
    loss_dur = 0
    for pred, inp, length in zip(s2s_preds, text_inputs, text_lengths):
        pred = pred[:length, :]
        inp = inp[:length].long()
        target = torch.zeros_like(pred)
        for i in range(target.shape[0]):
            target[i, : inp[i]] = 1
        dur_pred = torch.sigmoid(pred).sum(dim=1)
        loss_dur += F.l1_loss(dur_pred[1 : length - 1], inp[1 : length - 1])
        loss_ce += F.binary_cross_entropy_with_logits(pred.flatten(), target.flatten())
    n = len(text_lengths)
    return loss_ce / n, loss_dur / n


def scale_gradients(model: dict, thresh: float, scale: float) -> None:
    """
    Scales (and clips) gradients for the given model dictionary.
    """
    total_norm = {}
    for key in model.keys():
        total_norm[key] = 0.0
        parameters = [
            p for p in model[key].parameters() if p.grad is not None and p.requires_grad
        ]
        for p in parameters:
            total_norm[key] += p.grad.detach().data.norm(2).item() ** 2
        total_norm[key] = total_norm[key] ** 0.5
    if total_norm.get("predictor", 0) > thresh:
        for key in model.keys():
            for p in model[key].parameters():
                if p.grad is not None:
                    p.grad *= 1 / total_norm["predictor"]
    # Apply additional scaling to specific modules.
    for p in model["predictor"].duration_proj.parameters():
        if p.grad is not None:
            p.grad *= scale
    for p in model["predictor"].lstm.parameters():
        if p.grad is not None:
            p.grad *= scale
    for p in model["diffusion"].parameters():
        if p.grad is not None:
            p.grad *= scale


def optimizer_step(train, keys: List[str]) -> None:
    """
    Steps the optimizer for each module key in keys.
    """
    for key in keys:
        train.optimizer.step(key)


def log_and_save_checkpoint(
    train, epoch: int, current_step: int, prefix: str = "epoch_1st"
) -> None:
    """
    Logs metrics and saves a checkpoint.
    """
    state = {
        "net": {key: train.model[key].state_dict() for key in train.model},
        "optimizer": train.optimizer.state_dict(),
        "iters": train.iters,
        "val_loss": train.best_loss,
        "epoch": epoch,
    }
    if current_step == -1:
        filename = f"{prefix}_{epoch:05d}.pth"
    else:
        filename = f"{prefix}_{epoch:05d}_step_{current_step:09d}.pth"
    save_path = osp.join(train.log_dir, filename)
    torch.save(state, save_path)
    print(f"Saving checkpoint to {save_path}")


###############################################
# train_first
###############################################


# ... (the rest of your helper functions remain unchanged)


def train_first(
    i: int, batch, running_loss: float, iters: int, train, epoch: int
) -> Tuple[float, int]:
    """
    Training function for the first stage.
    """
    # --- Batch Preparation ---
    texts, input_lengths, mels, mel_input_length = prepare_batch(
        batch, train.device, ["texts", "input_lengths", "mels", "mel_input_length"]
    )

    # --- Alignment Computation ---
    s2s_attn, s2s_attn_mono, s2s_pred, asr, _, _ = compute_alignment(
        train,
        mels,
        texts,
        input_lengths,
        mel_input_length,
        apply_attention_mask=True,
        use_random_choice=True,
    )
    mel_gt = mels  # Ground truth mel spectrogram

    if mel_gt.shape[-1] < 40 or (
        mel_gt.shape[-1] < 80 and not train.model_params.skip_downsamples
    ):
        log_print("Skipping batch. TOO SHORT", train.logger)
        return running_loss, iters

    # --- Pitch Extraction ---
    with torch.no_grad():
        real_norm = log_norm(mel_gt.unsqueeze(1)).squeeze(1)
        F0_real, _, _ = train.model.pitch_extractor(mel_gt.unsqueeze(1))

    # --- Style Encoding & Decoding ---
    with train.accelerator.autocast():
        style_emb = train.model.style_encoder(
            mels.unsqueeze(1) if train.multispeaker else mel_gt.unsqueeze(1)
        )
        y_rec, mag_rec, phase_rec = train.model.decoder(
            asr, F0_real, real_norm, style_emb
        )

    # --- Waveform Preparation ---
    wav = prepare_batch(batch, train.device, ["waves"])[0]
    wav.requires_grad_(False)

    # --- Discriminator Loss ---
    if epoch >= train.TMA_epoch:
        train.optimizer.zero_grad()
        with train.accelerator.autocast():
            d_loss = train.dl(wav.detach().unsqueeze(1).float(), y_rec.detach()).mean()
        train.accelerator.backward(d_loss)
        optimizer_step(train, ["msd", "mpd"])
    else:
        d_loss = 0

    # --- Generator Loss ---
    train.optimizer.zero_grad()
    with train.accelerator.autocast():
        loss_mel = train.stft_loss(y_rec.squeeze(), wav.detach())
        loss_magphase = magphase_loss(mag_rec, phase_rec, wav.detach())
        if epoch >= train.TMA_epoch:
            loss_s2s = 0
            for _s2s_pred, _text_input, _text_length in zip(
                s2s_pred, texts, input_lengths
            ):
                loss_s2s += F.cross_entropy(
                    _s2s_pred[:_text_length], _text_input[:_text_length]
                )
            loss_s2s /= texts.size(0)
            loss_mono = F.l1_loss(s2s_attn, s2s_attn_mono) * 10
            loss_gen_all = train.gl(wav.detach().unsqueeze(1).float(), y_rec).mean()
            loss_slm = train.wl(wav.detach(), y_rec).mean()
            g_loss = (
                train.loss_params.lambda_mel * loss_mel
                + train.loss_params.lambda_mono * loss_mono
                + train.loss_params.lambda_s2s * loss_s2s
                + train.loss_params.lambda_gen * loss_gen_all
                + train.loss_params.lambda_slm * loss_slm
                + loss_magphase
            )
        else:
            g_loss = loss_mel + loss_magphase
    running_loss += loss_mel.item()
    train.accelerator.backward(g_loss)

    # --- Optimizer Steps ---
    optimizer_step(train, ["text_encoder", "style_encoder", "decoder"])
    if epoch >= train.TMA_epoch:
        optimizer_step(train, ["text_aligner", "pitch_extractor"])
    train.iters += 1

    # --- Logging ---
    if (i + 1) % train.log_interval == 0:
        metrics = {
            "mel_loss": running_loss / train.log_interval,
            "gen_loss": loss_gen_all if epoch >= train.TMA_epoch else loss_mel,
            "d_loss": d_loss,
            "mono_loss": loss_mono if epoch >= train.TMA_epoch else 0,
            "s2s_loss": loss_s2s if epoch >= train.TMA_epoch else 0,
            "slm_loss": loss_slm if epoch >= train.TMA_epoch else 0,
            "mp_loss": loss_magphase,
        }
        log_print(
            f"Epoch [{epoch}/{train.epochs}], Step [{i+1}/{train.batch_manager.get_step_count()}], "
            + ", ".join(f"{k}: {v:.5f}" for k, v in metrics.items()),
            train.logger,
        )
        for key, value in metrics.items():
            train.writer.add_scalar(f"train/{key}", value, train.iters)
        running_loss = 0
        print("Time elapsed:", time.time() - train.start_time)

    return running_loss, iters


###############################################
# train_second
###############################################


def train_second(
    i: int, batch, running_loss: float, iters: int, train, epoch: int
) -> Tuple[float, int]:
    """
    Training function for the second stage.
    """
    (
        waves,
        texts,
        input_lengths,
        ref_texts,
        ref_lengths,
        mels,
        mel_input_length,
        ref_mels,
    ) = prepare_batch(
        batch,
        train.device,
        [
            "waves",
            "texts",
            "input_lengths",
            "ref_texts",
            "ref_lengths",
            "mels",
            "mel_input_length",
            "ref_mels",
        ],
    )
    with torch.no_grad():
        mel_mask = length_to_mask(mel_input_length).to(train.device)
    try:
        _, s2s_attn_mono, _, asr, text_mask, _ = compute_alignment(
            train,
            mels,
            texts,
            input_lengths,
            mel_input_length,
            apply_attention_mask=False,
            use_random_choice=False,
        )
    except Exception as e:
        print(f"s2s_attn computation failed: {e}")
        return running_loss, iters

    d_gt = s2s_attn_mono.sum(axis=-1).detach()

    if train.multispeaker and epoch >= train.diff_epoch:
        with train.accelerator.autocast():
            ref_ss = train.model.style_encoder(ref_mels.unsqueeze(1))
            ref_sp = train.model.predictor_encoder(ref_mels.unsqueeze(1))
            ref = torch.cat([ref_ss, ref_sp], dim=1)
    else:
        ref = None

    with train.accelerator.autocast():
        s_dur = train.model.predictor_encoder(mels.unsqueeze(1))
        gs = train.model.style_encoder(mels.unsqueeze(1))
        s_trg = torch.cat([gs, s_dur], dim=-1).detach()  # ground truth for denoiser
        bert_dur = train.model.bert(texts, attention_mask=(~text_mask).int())
        d_en = train.model.bert_encoder(bert_dur).transpose(-1, -2)

    if epoch >= train.diff_epoch:
        num_steps = np.random.randint(3, 5)
        with torch.no_grad():
            if train.model_params.diffusion.dist.estimate_sigma_data:
                sigma_data = s_trg.std(axis=-1).mean().item()
                train.model.diffusion.module.diffusion.sigma_data = sigma_data
                train.running_std.append(sigma_data)
        with train.accelerator.autocast():
            noise = torch.randn_like(s_trg).unsqueeze(1).to(train.device)
            if train.multispeaker:
                s_preds = train.sampler(
                    noise=noise,
                    embedding=bert_dur,
                    embedding_scale=1,
                    features=ref,
                    embedding_mask_proba=0.1,
                    num_steps=num_steps,
                ).squeeze(1)
                loss_diff = train.model.diffusion(
                    s_trg.unsqueeze(1), embedding=bert_dur, features=ref
                ).mean()
                loss_sty = F.l1_loss(s_preds, s_trg.detach())
            else:
                s_preds = train.sampler(
                    noise=noise,
                    embedding=bert_dur,
                    embedding_scale=1,
                    embedding_mask_proba=0.1,
                    num_steps=num_steps,
                ).squeeze(1)
                loss_diff = train.model.diffusion.module.diffusion(
                    s_trg.unsqueeze(1), embedding=bert_dur
                ).mean()
                loss_sty = F.l1_loss(s_preds, s_trg.detach())
    else:
        loss_sty = 0
        loss_diff = 0

    with train.accelerator.autocast():
        d, p_en = train.model.predictor(
            d_en, s_dur, input_lengths, s2s_attn_mono, text_mask
        )

    wav = waves  # Assume already on train.device
    if mels.shape[-1] < 40 or (
        mels.shape[-1] < 80 and not train.model_params.skip_downsamples
    ):
        log_print("Skipping batch. TOO SHORT", train.logger)
        return running_loss, iters

    with torch.no_grad():
        F0_real, _, _ = train.model.pitch_extractor(mels.unsqueeze(1))
        N_real = log_norm(mels.unsqueeze(1)).squeeze(1)
        wav = wav.unsqueeze(1)
        y_rec_gt = wav
        if epoch >= train.joint_epoch:
            with train.accelerator.autocast():
                y_rec_gt_pred, _, _ = train.model.decoder(asr, F0_real, N_real, gs)

    with train.accelerator.autocast():
        F0_fake, N_fake = train.model.predictor.F0Ntrain(p_en, s_dur)
        y_rec, mag_rec, phase_rec = train.model.decoder(asr, F0_fake, N_fake, gs)
        loss_magphase = magphase_loss(mag_rec, phase_rec, wav.squeeze(1).detach())

    loss_F0_rec = F.smooth_l1_loss(F0_real, F0_fake) / 10
    loss_norm_rec = F.smooth_l1_loss(N_real, N_fake)

    if train.start_ds:
        train.optimizer.zero_grad()
        d_loss = train.dl(wav.detach(), y_rec.detach()).mean()
        d_loss.backward()
        optimizer_step(train, ["msd", "mpd"])
    else:
        d_loss = 0

    train.optimizer.zero_grad()
    with train.accelerator.autocast():
        loss_mel = train.stft_loss(y_rec, wav)
        loss_gen_all = train.gl(wav, y_rec).mean() if train.start_ds else 0
        loss_lm = train.wl(wav.detach().squeeze(1), y_rec.squeeze(1)).mean()

    loss_ce, loss_dur = compute_duration_ce_loss(d, d_gt, input_lengths)

    g_loss = (
        train.loss_params.lambda_mel * loss_mel
        + train.loss_params.lambda_F0 * loss_F0_rec
        + train.loss_params.lambda_ce * loss_ce
        + train.loss_params.lambda_norm * loss_norm_rec
        + train.loss_params.lambda_dur * loss_dur
        + train.loss_params.lambda_gen * loss_gen_all
        + train.loss_params.lambda_slm * loss_lm
        + train.loss_params.lambda_sty * loss_sty
        + train.loss_params.lambda_diff * loss_diff
        + loss_magphase
    )

    running_loss += loss_mel.item()
    train.accelerator.backward(g_loss)

    optimizer_step(train, ["bert_encoder", "bert", "predictor", "predictor_encoder"])
    if epoch >= train.diff_epoch:
        optimizer_step(train, ["diffusion"])
    if epoch >= train.joint_epoch or train.early_joint:
        optimizer_step(train, ["style_encoder", "decoder"])

    if epoch >= train.joint_epoch:
        use_ind = np.random.rand() < 0.5
        if use_ind:
            ref_lengths = input_lengths
            ref_texts = texts
        slm_out = train.slmadv(
            i,
            y_rec_gt,
            y_rec_gt_pred if epoch >= train.joint_epoch else None,
            waves,
            mel_input_length,
            ref_texts,
            ref_lengths,
            use_ind,
            s_trg.detach(),
            ref if train.multispeaker else None,
        )
        if slm_out is None:
            print("slm_out none")
            return running_loss, iters

        d_loss_slm, loss_gen_lm, y_pred = slm_out
        train.optimizer.zero_grad()
        loss_gen_lm.backward()
        scale_gradients(
            train.model, train.slmadv_params.thresh, train.slmadv_params.scale
        )
        optimizer_step(train, ["bert_encoder", "bert", "predictor", "diffusion"])
        if d_loss_slm != 0:
            train.optimizer.zero_grad()
            d_loss_slm.backward(retain_graph=True)
            train.optimizer.step("wd")
    else:
        d_loss_slm, loss_gen_lm = 0, 0

    train.iters += 1

    if (i + 1) % train.log_interval == 0:
        metrics = {
            "mel_loss": running_loss / train.log_interval,
            "d_loss": d_loss,
            "ce_loss": loss_ce,
            "dur_loss": loss_dur,
            "norm_loss": loss_norm_rec,
            "F0_loss": loss_F0_rec,
            "lm_loss": loss_lm,
            "gen_loss": loss_gen_all,
            "sty_loss": loss_sty,
            "diff_loss": loss_diff,
            "d_loss_slm": d_loss_slm,
            "gen_loss_slm": loss_gen_lm,
            "mp_loss": loss_magphase,
        }
        train.logger.info(
            f"Epoch [{epoch}/{train.epochs}], Step [{i+1}/{train.batch_manager.get_step_count()}], "
            + ", ".join(f"{k}: {v:.5f}" for k, v in metrics.items())
        )
        for key, value in metrics.items():
            train.writer.add_scalar(f"train/{key}", value, train.iters)
        running_loss = 0
        print("Time elapsed:", time.time() - train.start_time)

    return running_loss, iters


###############################################
# validate_first
###############################################


def validate_first(current_epoch: int, current_step: int, save: bool, train) -> None:
    """
    Validation function for the first stage.
    """
    loss_test = 0
    # Set models to evaluation mode.
    for key in train.model:
        train.model[key].eval()

    with torch.no_grad():
        iters_test = 0
        for batch in train.val_dataloader:
            waves, texts, input_lengths, mels, mel_input_length = prepare_batch(
                batch,
                train.device,
                ["waves", "texts", "input_lengths", "mels", "mel_input_length"],
            )
            mask = length_to_mask(mel_input_length // (2**train.n_down)).to(
                train.device
            )
            text_mask = length_to_mask(input_lengths).to(train.device)
            _, _, s2s_attn = train.model.text_aligner(mels, mask, texts)
            s2s_attn = s2s_attn.transpose(-1, -2)
            s2s_attn = s2s_attn[..., 1:]
            s2s_attn = s2s_attn.transpose(-1, -2)
            mask_ST = mask_from_lens(
                s2s_attn, input_lengths, mel_input_length // (2**train.n_down)
            )
            s2s_attn_mono = maximum_path(s2s_attn, mask_ST)
            t_en = train.model.text_encoder(texts, input_lengths, text_mask)
            asr = t_en @ s2s_attn_mono

            if mels.shape[-1] < 40 or (
                mels.shape[-1] < 80 and not train.model_params.skip_downsamples
            ):
                log_print("Skipping batch. TOO SHORT", train.logger)
                continue

            F0_real, _, _ = train.model.pitch_extractor(mels.unsqueeze(1))
            s = train.model.style_encoder(mels.unsqueeze(1))
            real_norm = log_norm(mels.unsqueeze(1)).squeeze(1)
            y_rec, _, _ = train.model.decoder(asr, F0_real, real_norm, s)
            loss_mel = train.stft_loss(y_rec.squeeze(), waves.detach())
            loss_test += loss_mel.item()
            iters_test += 1

    avg_loss = loss_test / iters_test if iters_test > 0 else float("inf")
    print(
        f"Epochs:{current_epoch} Steps:{current_step} Loss:{avg_loss} Best_Loss:{train.best_loss}"
    )
    log_print(
        f"Epochs:{current_epoch} Steps:{current_step} Loss:{avg_loss} Best_Loss:{train.best_loss}",
        train.logger,
    )
    log_print(f"Validation loss: {avg_loss:.3f}\n\n\n\n", train.logger)
    train.writer.add_scalar("eval/mel_loss", avg_loss, current_epoch)
    attn_image = get_image(s2s_attn[0].cpu().numpy().squeeze())
    train.writer.add_figure("eval/attn", attn_image, current_epoch)

    with torch.no_grad():
        for bib in range(min(len(asr), 6)):
            mel_length = int(mel_input_length[bib].item())
            gt = mels[bib, :, :mel_length].unsqueeze(0)
            en = asr[bib, :, : mel_length // 2].unsqueeze(0)
            F0_real, _, _ = train.model.pitch_extractor(gt.unsqueeze(1))
            s = train.model.style_encoder(gt.unsqueeze(1))
            real_norm = log_norm(gt.unsqueeze(1)).squeeze(1)
            y_rec, _, _ = train.model.decoder(en, F0_real, real_norm, s)
            train.writer.add_audio(
                f"eval/y{bib}",
                y_rec.cpu().numpy().squeeze(),
                current_epoch,
                sample_rate=train.sr,
            )
            if current_epoch == 0:
                train.writer.add_audio(
                    f"gt/y{bib}",
                    waves[bib].squeeze(),
                    current_epoch,
                    sample_rate=train.sr,
                )

    if current_epoch % train.saving_epoch == 0 and save and current_step == -1:
        if avg_loss < train.best_loss:
            train.best_loss = avg_loss
        print("Saving..")
        log_and_save_checkpoint(train, current_epoch, current_step, prefix="epoch_1st")
    if save and current_step != -1:
        if avg_loss < train.best_loss:
            train.best_loss = avg_loss
        print("Saving..")
        log_and_save_checkpoint(train, current_epoch, current_step, prefix="epoch_1st")

    for key in train.model:
        train.model[key].train()


###############################################
# validate_second
###############################################


def validate_second(current_epoch: int, current_step: int, save: bool, train) -> None:
    """
    Validation function for the second stage.
    """
    loss_test = 0
    loss_align = 0
    loss_f = 0
    for key in train.model:
        train.model[key].eval()

    with torch.no_grad():
        iters_test = 0
        for batch in train.val_dataloader:
            try:
                waves, texts, input_lengths, mels, mel_input_length, ref_mels = (
                    prepare_batch(
                        batch,
                        train.device,
                        [
                            "waves",
                            "texts",
                            "input_lengths",
                            "mels",
                            "mel_input_length",
                            "ref_mels",
                        ],
                    )
                )
                mask = length_to_mask(mel_input_length // (2**train.n_down)).to(
                    train.device
                )
                text_mask = length_to_mask(input_lengths).to(train.device)
                _, _, s2s_attn = train.model.text_aligner(mels, mask, texts)
                s2s_attn = s2s_attn.transpose(-1, -2)
                s2s_attn = s2s_attn[..., 1:]
                s2s_attn = s2s_attn.transpose(-1, -2)
                mask_ST = mask_from_lens(
                    s2s_attn, input_lengths, mel_input_length // (2**train.n_down)
                )
                s2s_attn_mono = maximum_path(s2s_attn, mask_ST)
                t_en = train.model.text_encoder(texts, input_lengths, text_mask)
                asr = t_en @ s2s_attn_mono
                # d_gt is computed here but not used further.
                d_gt = s2s_attn_mono.sum(axis=-1).detach()
                if mels.shape[-1] < 40 or (
                    mels.shape[-1] < 80 and not train.model_params.skip_downsamples
                ):
                    log_print("Skipping batch. TOO SHORT", train.logger)
                    continue
                s = train.model.predictor_encoder(mels.unsqueeze(1))
                gs = train.model.style_encoder(mels.unsqueeze(1))
                s_trg = torch.cat([s, gs], dim=-1).detach()
                bert_dur = train.model.bert(texts, attention_mask=(~text_mask).int())
                d_en = train.model.bert_encoder(bert_dur).transpose(-1, -2)
                d, p = train.model.predictor(
                    d_en, s, input_lengths, s2s_attn_mono, text_mask
                )
                F0_fake, N_fake = train.model.predictor.F0Ntrain(p, s)
                loss_dur = 0
                for pred, inp, length in zip(d, d_gt, input_lengths):
                    pred = pred[:length, :]
                    inp = inp[:length].long()
                    target = torch.zeros_like(pred)
                    for i in range(target.shape[0]):
                        target[i, : inp[i]] = 1
                    dur_pred = torch.sigmoid(pred).sum(dim=1)
                    loss_dur += F.l1_loss(dur_pred[1 : length - 1], inp[1 : length - 1])
                loss_dur /= texts.size(0)
                y_rec, _, _ = train.model.decoder(asr, F0_fake, N_fake, gs)
                loss_mel = train.stft_loss(y_rec.squeeze(1), waves.detach())
                F0_real, _, _ = train.model.pitch_extractor(mels.unsqueeze(1))
                loss_F0 = F.l1_loss(F0_real, F0_fake) / 10
                loss_test += loss_mel.mean()
                loss_align += loss_dur.mean()
                loss_f += loss_F0.mean()
                iters_test += 1
            except Exception as e:
                print(f"Encountered exception: {e}")
                traceback.print_exc()
                continue

    avg_loss = loss_test / iters_test if iters_test > 0 else float("inf")
    print(
        f"Epochs: {current_epoch}, Steps: {current_step}, Loss: {avg_loss}, Best_Loss: {train.best_loss}"
    )
    train.logger.info(
        f"Validation loss: {avg_loss:.3f}, Dur loss: {loss_align / iters_test:.3f}, F0 loss: {loss_f / iters_test:.3f}\n\n\n"
    )
    train.writer.add_scalar("eval/mel_loss", avg_loss, current_epoch)
    attn_image = get_image(s2s_attn[0].cpu().numpy().squeeze())
    train.writer.add_figure("eval/attn", attn_image, current_epoch)

    with torch.no_grad():
        for bib in range(min(len(asr), 6)):
            mel_length = int(mel_input_length[bib].item())
            gt = mels[bib, :, :mel_length].unsqueeze(0)
            en = asr[bib, :, : mel_length // 2].unsqueeze(0)
            F0_real, _, _ = train.model.pitch_extractor(gt.unsqueeze(1))
            s = train.model.style_encoder(gt.unsqueeze(1))
            real_norm = log_norm(gt.unsqueeze(1)).squeeze(1)
            y_rec, _, _ = train.model.decoder(en, F0_real, real_norm, s)
            train.writer.add_audio(
                f"eval/y{bib}",
                y_rec.cpu().numpy().squeeze(),
                current_epoch,
                sample_rate=train.sr,
            )
            if current_epoch == 0:
                train.writer.add_audio(
                    f"gt/y{bib}",
                    waves[bib].squeeze(),
                    current_epoch,
                    sample_rate=train.sr,
                )
    if current_epoch % train.saving_epoch == 0 and save and current_step == -1:
        if avg_loss < train.best_loss:
            train.best_loss = avg_loss
        print("Saving..")
        log_and_save_checkpoint(train, current_epoch, current_step, prefix="epoch_1st")
    if save and current_step != -1:
        if avg_loss < train.best_loss:
            train.best_loss = avg_loss
        print("Saving..")
        log_and_save_checkpoint(train, current_epoch, current_step, prefix="epoch_1st")
    for key in train.model:
        train.model[key].train()

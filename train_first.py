import os
import os.path as osp
import re
import sys
import json
import yaml
import shutil
import numpy as np
import torch
import click
import warnings

warnings.simplefilter("ignore")

# load packages
import random
import yaml
from munch import Munch
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
import torchaudio
import librosa

from models import *
from meldataset import build_dataloader, BatchManager
from utils import *
from losses import *
from optimizers import build_optimizer
import time

from accelerate import Accelerator
from accelerate.utils import LoggerType
from accelerate import DistributedDataParallelKwargs

from torch.utils.tensorboard import SummaryWriter

import logging
from accelerate.logging import get_logger

logger = get_logger(__name__, log_level="DEBUG")


@click.command()
@click.option("-p", "--config_path", default="Configs/config.yml", type=str)
@click.option("--probe_batch", default=None, type=int)
def main(config_path, probe_batch):
    config = yaml.safe_load(open(config_path))

    log_dir = config["log_dir"]
    if not osp.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    shutil.copy(config_path, osp.join(log_dir, osp.basename(config_path)))
    ddp_kwargs = DistributedDataParallelKwargs(find_unused_parameters=True)
    accelerator = Accelerator(
        project_dir=log_dir, split_batches=True, kwargs_handlers=[ddp_kwargs]
    )
    if accelerator.is_main_process:
        writer = SummaryWriter(log_dir + "/tensorboard")

    # write logs
    file_handler = logging.FileHandler(osp.join(log_dir, "train.log"))
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(levelname)s:%(asctime)s: %(message)s")
    )
    logger.logger.addHandler(file_handler)

    device = accelerator.device

    epochs = config.get("epochs_1st", 200)
    save_freq = config.get("save_freq", 2)
    log_interval = config.get("log_interval", 10)
    saving_epoch = config.get("save_freq", 2)

    debug = config.get("debug", False)
    val_interval = config.get("val_interval", 1000)
    save_interval = config.get("save_interval", 2000)

    data_params = config.get("data_params", None)
    sr = config["preprocess_params"].get("sr", 24000)
    train_path = data_params["train_data"]
    val_path = data_params["val_data"]
    root_path = data_params["root_path"]
    min_length = data_params["min_length"]
    OOD_data = data_params["OOD_data"]

    if not osp.exists(train_path):
        print("Train data not found at {}".format(train_path))
        exit(1)
    if not osp.exists(val_path):
        print("Validation data not found at {}".format(val_path))
        exit(1)
    if not osp.exists(root_path):
        print("Root path not found at {}".format(root_path))
        exit(1)

    if "skip_downsamples" not in config["model_params"]:
        config["model_params"]["skip_downsamples"] = False
    model_params = recursive_munch(config["model_params"])
    multispeaker = model_params.multispeaker

    # load data
    val_list = get_data_path_list(val_path)
    val_dataloader = build_dataloader(
        val_list,
        root_path,
        OOD_data=OOD_data,
        min_length=min_length,
        batch_size={},
        validation=True,
        num_workers=0,
        device=device,
        dataset_config={},
        multispeaker=multispeaker,
    )

    def log_print_function(s):
        log_print(s, logger)

    batch_manager = BatchManager(
        train_path,
        log_dir,
        probe_batch=probe_batch,
        root_path=root_path,
        OOD_data=OOD_data,
        min_length=min_length,
        device=device,
        accelerator=accelerator,
        log_print=log_print_function,
        multispeaker=multispeaker,
    )

    with accelerator.main_process_first():
        # load pretrained ASR model
        ASR_config = config.get("ASR_config", False)
        ASR_path = config.get("ASR_path", False)
        text_aligner = load_ASR_models(ASR_path, ASR_config)

        # load pretrained F0 model
        F0_path = config.get("F0_path", False)
        pitch_extractor = load_F0_models(F0_path)

        # load BERT model
        from Utils.PLBERT.util import load_plbert

        BERT_path = config.get("PLBERT_dir", False)
        plbert = load_plbert(BERT_path)

    scheduler_params = {
        "max_lr": float(config["optimizer_params"].get("lr", 1e-4)),
        "pct_start": float(config["optimizer_params"].get("pct_start", 0.0)),
        "epochs": epochs,
        "steps_per_epoch": batch_manager.get_step_count(),
    }

    model = build_model(model_params, text_aligner, pitch_extractor, plbert)
    global best_loss
    best_loss = float("inf")  # best test loss
    loss_train_record = list([])
    loss_test_record = list([])

    loss_params = Munch(config["loss_params"])
    TMA_epoch = loss_params.TMA_epoch

    for k in model:
        model[k] = accelerator.prepare(model[k])

    val_dataloader = accelerator.prepare(val_dataloader)

    _ = [model[key].to(device) for key in model]

    # initialize optimizers after preparing models for compatibility with FSDP
    optimizer = build_optimizer(
        {key: model[key].parameters() for key in model},
        scheduler_params_dict={key: scheduler_params.copy() for key in model},
        lr=float(config["optimizer_params"].get("lr", 1e-4)),
    )

    for k, v in optimizer.optimizers.items():
        optimizer.optimizers[k] = accelerator.prepare(optimizer.optimizers[k])
        optimizer.schedulers[k] = accelerator.prepare(optimizer.schedulers[k])

    with accelerator.main_process_first():
        if config.get("pretrained_model", "") != "":
            model, optimizer, start_epoch, iters = load_checkpoint(
                model,
                optimizer,
                config["pretrained_model"],
                load_only_params=config.get("load_only_params", True),
            )
            start_epoch += 1
        else:
            start_epoch = 1
            iters = 0

    # in case not distributed
    try:
        n_down = model.text_aligner.module.n_down
    except:
        n_down = model.text_aligner.n_down

    # wrapped losses for compatibility with mixed precision
    stft_loss = MultiResolutionSTFTLoss().to(device)
    gl = GeneratorLoss(model.mpd, model.msd).to(device)
    dl = DiscriminatorLoss(model.mpd, model.msd).to(device)
    wl = WavLMLoss(model_params.slm.model, model.wd, sr, model_params.slm.sr).to(device)

    for epoch in range(start_epoch, epochs):
        running_loss = 0
        start_time = time.time()

        _ = [model[key].train() for key in model]

        def validation(current_epoch: int, current_step: int, save: bool):
            global best_loss
            loss_test = 0
            max_len = 1620

            _ = [model[key].eval() for key in model]

            with torch.no_grad():
                iters_test = 0
                for batch_idx, batch in enumerate(val_dataloader):
                    optimizer.zero_grad()

                    waves = batch[0]
                    batch = [b.to(device) for b in batch[1:]]
                    texts, input_lengths, _, _, mels, mel_input_length, _ = batch

                    with torch.no_grad():
                        mask = length_to_mask(mel_input_length // (2**n_down)).to(
                            "cuda"
                        )
                        ppgs, s2s_pred, s2s_attn = model.text_aligner(mels, mask, texts)

                        s2s_attn = s2s_attn.transpose(-1, -2)
                        s2s_attn = s2s_attn[..., 1:]
                        s2s_attn = s2s_attn.transpose(-1, -2)

                        text_mask = length_to_mask(input_lengths).to(texts.device)
                        attn_mask = (
                            (~mask)
                            .unsqueeze(-1)
                            .expand(mask.shape[0], mask.shape[1], text_mask.shape[-1])
                            .float()
                            .transpose(-1, -2)
                        )
                        attn_mask = (
                            attn_mask.float()
                            * (~text_mask)
                            .unsqueeze(-1)
                            .expand(
                                text_mask.shape[0], text_mask.shape[1], mask.shape[-1]
                            )
                            .float()
                        )
                        attn_mask = attn_mask < 1
                        s2s_attn.masked_fill_(attn_mask, 0.0)

                    # encode
                    t_en = model.text_encoder(texts, input_lengths, text_mask)

                    asr = t_en @ s2s_attn

                    # get clips
                    mel_input_length_all = accelerator.gather(
                        mel_input_length
                    )  # for balanced load
                    mel_len = min(
                        [int(mel_input_length.min().item() / 2 - 1), max_len // 2]
                    )

                    en = []
                    gt = []
                    wav = []
                    for bib in range(len(mel_input_length)):
                        mel_length = int(mel_input_length[bib].item() / 2)

                        random_start = np.random.randint(0, mel_length - mel_len)
                        en.append(asr[bib, :, random_start : random_start + mel_len])
                        gt.append(
                            mels[
                                bib,
                                :,
                                (random_start * 2) : ((random_start + mel_len) * 2),
                            ]
                        )
                        y = waves[bib][
                            (random_start * 2)
                            * 300 : ((random_start + mel_len) * 2)
                            * 300
                        ]
                        wav.append(torch.from_numpy(y).to("cuda"))

                    wav = torch.stack(wav).float().detach()

                    en = torch.stack(en)
                    gt = torch.stack(gt).detach()

                    # clip too short to be used by the style encoder
                    if gt.shape[-1] < 40 or (
                        gt.shape[-1] < 80 and not model_params.skip_downsamples
                    ):
                        log_print("Skipping batch. TOO SHORT", logger)
                        continue

                    F0_real, _, F0 = model.pitch_extractor(gt.unsqueeze(1))
                    s = model.style_encoder(gt.unsqueeze(1))
                    real_norm = log_norm(gt.unsqueeze(1)).squeeze(1)
                    y_rec, _, _ = model.decoder(en, F0_real, real_norm, s)

                    loss_mel = stft_loss(y_rec.squeeze(), wav.detach())

                    loss_test += accelerator.gather(loss_mel).mean().item()
                    iters_test += 1
            if accelerator.is_main_process:
                print(
                    f"Epochs:{current_epoch} Steps:{current_step} Loss:{loss_test / iters_test} Best_Loss:{best_loss}"
                )
                log_print(
                    f"Epochs:{current_epoch} Steps:{current_step} Loss:{loss_test / iters_test} Best_Loss:{best_loss}",
                    logger,
                )
                log_print(
                    "Validation loss: %.3f" % (loss_test / iters_test) + "\n\n\n\n",
                    logger,
                )
                print("\n\n\n")
                writer.add_scalar(
                    "eval/mel_loss", loss_test / iters_test, current_epoch
                )
                attn_image = get_image(s2s_attn[0].cpu().numpy().squeeze())
                writer.add_figure("eval/attn", attn_image, current_epoch)

                with torch.no_grad():
                    for bib in range(len(asr)):
                        mel_length = int(mel_input_length[bib].item())
                        gt = mels[bib, :, :mel_length].unsqueeze(0)
                        en = asr[bib, :, : mel_length // 2].unsqueeze(0)

                        F0_real, _, _ = model.pitch_extractor(gt.unsqueeze(1))
                        # F0_real = F0_real.unsqueeze(0)
                        s = model.style_encoder(gt.unsqueeze(1))
                        real_norm = log_norm(gt.unsqueeze(1)).squeeze(1)

                        y_rec, _, _ = model.decoder(en, F0_real, real_norm, s)

                        writer.add_audio(
                            "eval/y" + str(bib),
                            y_rec.cpu().numpy().squeeze(),
                            current_epoch,
                            sample_rate=sr,
                        )
                        if current_epoch == 0:
                            writer.add_audio(
                                "gt/y" + str(bib),
                                waves[bib].squeeze(),
                                current_epoch,
                                sample_rate=sr,
                            )

                        if bib >= 6:
                            break

                if current_epoch % saving_epoch == 0 and save and current_step == -1:
                    if (loss_test / iters_test) < best_loss:
                        best_loss = loss_test / iters_test
                    print("Saving..")
                    state = {
                        "net": {key: model[key].state_dict() for key in model},
                        "optimizer": optimizer.state_dict(),
                        "iters": iters,
                        "val_loss": loss_test / iters_test,
                        "epoch": current_epoch,
                    }
                    save_path = osp.join(log_dir, f"epoch_1st_{current_epoch:>05}.pth")
                    torch.save(state, save_path)

                if save and current_step != -1:
                    if (loss_test / iters_test) < best_loss:
                        best_loss = loss_test / iters_test
                    print("Saving..")
                    state = {
                        "net": {key: model[key].state_dict() for key in model},
                        "optimizer": optimizer.state_dict(),
                        "iters": iters,
                        "val_loss": loss_test / iters_test,
                        "epoch": current_epoch,
                    }
                    save_path = osp.join(
                        log_dir,
                        f"epoch_1st_{current_epoch:>05}_step_{current_step:>09}.pth",
                    )
                    torch.save(state, save_path)
            _ = [model[key].train() for key in model]  # Restore training mode

        def train_batch(i, batch, running_loss, iters):
            try:
                waves = batch[0]
                batch = [b.to(device) for b in batch[1:]]
                texts, input_lengths, _, _, mels, mel_input_length, _ = batch

                with torch.no_grad():
                    mask = length_to_mask(mel_input_length // (2**n_down)).to("cuda")
                    text_mask = length_to_mask(input_lengths).to(texts.device)

                ppgs, s2s_pred, s2s_attn = model.text_aligner(mels, mask, texts)

                s2s_attn = s2s_attn.transpose(-1, -2)
                s2s_attn = s2s_attn[..., 1:]
                s2s_attn = s2s_attn.transpose(-1, -2)

                with torch.no_grad():
                    attn_mask = (
                        (~mask)
                        .unsqueeze(-1)
                        .expand(mask.shape[0], mask.shape[1], text_mask.shape[-1])
                        .float()
                        .transpose(-1, -2)
                    )
                    attn_mask = (
                        attn_mask.float()
                        * (~text_mask)
                        .unsqueeze(-1)
                        .expand(text_mask.shape[0], text_mask.shape[1], mask.shape[-1])
                        .float()
                    )
                    attn_mask = attn_mask < 1

                s2s_attn.masked_fill_(attn_mask, 0.0)

                with torch.no_grad():
                    mask_ST = mask_from_lens(
                        s2s_attn, input_lengths, mel_input_length // (2**n_down)
                    )
                    s2s_attn_mono = maximum_path(s2s_attn, mask_ST)

                # encode
                t_en = model.text_encoder(texts, input_lengths, text_mask)

                # 50% of chance of using monotonic version
                if bool(random.getrandbits(1)):
                    asr = t_en @ s2s_attn
                else:
                    asr = t_en @ s2s_attn_mono

                # get clips
                # mel_input_length_all = accelerator.gather(mel_input_length) # for balanced load
                # mel_len = min([int(mel_input_length_all.min().item() / 2 - 1), max_len // 2])
                # mel_len_st = int(mel_input_length.min().item() / 2 - 1)

                en = []
                gt = []
                wav = []
                st = []

                for bib in range(len(mel_input_length)):
                    en.append(asr[bib])
                    gt.append(mels[bib])

                    y = waves[bib]
                    wav.append(torch.from_numpy(y).to(device))

                    # style reference (better to be different from the GT)
                    st.append(mels[bib])

                en = torch.stack(en)
                gt = torch.stack(gt).detach()
                st = torch.stack(st).detach()

                wav = torch.stack(wav).float().detach()

                # clip too short to be used by the style encoder
                if gt.shape[-1] < 40 or (
                    gt.shape[-1] < 80 and not model_params.skip_downsamples
                ):
                    log_print("Skipping batch. TOO SHORT", logger)
                    return running_loss, iters

                with torch.no_grad():
                    real_norm = log_norm(gt.unsqueeze(1)).squeeze(1).detach()
                    F0_real, _, _ = model.pitch_extractor(gt.unsqueeze(1))

                s = model.style_encoder(
                    st.unsqueeze(1) if multispeaker else gt.unsqueeze(1)
                )

                y_rec, mag_rec, phase_rec = model.decoder(en, F0_real, real_norm, s)

                # discriminator loss

                if epoch >= TMA_epoch:
                    optimizer.zero_grad()
                    d_loss = dl(
                        wav.detach().unsqueeze(1).float(), y_rec.detach()
                    ).mean()
                    accelerator.backward(d_loss)
                    optimizer.step("msd")
                    optimizer.step("mpd")
                else:
                    d_loss = 0

                # generator loss
                optimizer.zero_grad()
                loss_mel = stft_loss(y_rec.squeeze(), wav.detach())
                loss_magphase = magphase_loss(mag_rec, phase_rec, wav.detach())
                if epoch >= TMA_epoch:  # start TMA training
                    loss_s2s = 0
                    for _s2s_pred, _text_input, _text_length in zip(
                        s2s_pred, texts, input_lengths
                    ):
                        loss_s2s += F.cross_entropy(
                            _s2s_pred[:_text_length], _text_input[:_text_length]
                        )
                    loss_s2s /= texts.size(0)

                    loss_mono = F.l1_loss(s2s_attn, s2s_attn_mono) * 10

                    loss_gen_all = gl(wav.detach().unsqueeze(1).float(), y_rec).mean()
                    loss_slm = wl(wav.detach(), y_rec).mean()

                    g_loss = (
                        loss_params.lambda_mel * loss_mel
                        + loss_params.lambda_mono * loss_mono
                        + loss_params.lambda_s2s * loss_s2s
                        + loss_params.lambda_gen * loss_gen_all
                        + loss_params.lambda_slm * loss_slm
                        + 1 * loss_magphase
                    )

                else:
                    loss_s2s = 0
                    loss_mono = 0
                    loss_gen_all = 0
                    loss_slm = 0
                    g_loss = loss_mel + loss_magphase

                running_loss += accelerator.gather(loss_mel).mean().item()

                accelerator.backward(g_loss)

                optimizer.step("text_encoder")
                optimizer.step("style_encoder")
                optimizer.step("decoder")

                if epoch >= TMA_epoch:
                    optimizer.step("text_aligner")
                    optimizer.step("pitch_extractor")

                iters = iters + 1

                if (i + 1) % log_interval == 0 and accelerator.is_main_process:
                    log_print(
                        "Epoch [%d/%d], Step [%d/%d], Mel Loss: %.5f, Gen Loss: %.5f, Disc Loss: %.5f, Mono Loss: %.5f, S2S Loss: %.5f, SLM Loss: %.5f, MP Loss: %.5f"
                        % (
                            epoch,
                            epochs,
                            i + 1,
                            batch_manager.get_step_count(),
                            running_loss / log_interval,
                            loss_gen_all,
                            d_loss,
                            loss_mono,
                            loss_s2s,
                            loss_slm,
                            loss_magphase,
                        ),
                        logger,
                    )

                    writer.add_scalar(
                        "train/mel_loss", running_loss / log_interval, iters
                    )
                    writer.add_scalar("train/gen_loss", loss_gen_all, iters)
                    writer.add_scalar("train/d_loss", d_loss, iters)
                    writer.add_scalar("train/mono_loss", loss_mono, iters)
                    writer.add_scalar("train/s2s_loss", loss_s2s, iters)
                    writer.add_scalar("train/slm_loss", loss_slm, iters)
                    writer.add_scalar("train/mp_loss", loss_magphase, iters)

                    running_loss = 0

                    print("Time elasped:", time.time() - start_time)
                if (i + 1) % val_interval == 0 or (i + 1) % save_interval == 0:
                    save = (i + 1) % save_interval == 0
                    validation(epoch, i + 1, save)

            except Exception as e:
                optimizer.zero_grad()
                raise e
            return running_loss, iters

        batch_manager.epoch_loop(epoch, train_batch, debug)

        validation(epoch, -1, True)

    if accelerator.is_main_process:
        print("Saving..")
        state = {
            "net": {key: model[key].state_dict() for key in model},
            "optimizer": optimizer.state_dict(),
            "iters": iters,
            "val_loss": loss_test / iters_test,
            "epoch": epoch,
        }
        save_path = osp.join(log_dir, config.get("first_stage_path", "first_stage.pth"))
        torch.save(state, save_path)


if __name__ == "__main__":
    main()

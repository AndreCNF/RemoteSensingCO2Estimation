import os
import numpy as np
import pandas as pd
import torch
from torch import nn, optim
from tqdm.autonotebook import tqdm
from torch.utils.data import DataLoader, RandomSampler

import argparse
from sklearn.metrics import jaccard_score

from models.model_multitask import *
from dataset_multitask import create_dataset

print("running on...", device)


def multi_acc(y_pred, y_test):
    y_pred_softmax = torch.log_softmax(y_pred, dim=1)
    _, y_pred_tags = torch.max(y_pred_softmax, dim=1)

    correct_pred = (y_pred_tags == y_test).float()
    acc = correct_pred.sum() / len(correct_pred)

    return acc


def train_model(
    model, params, opt, channels, datadir, seglabeldir, reg_data, checkpoint_dir
):
    """Wrapper function for model training.
    :param model: model instance
    :param params: parameters
    :param datadir: path to satellite images
    :param opt: optimizer instance
    :param channels: list of channels indices
    :param checkpoint_dir: path to model checkpoints
    :param seglabeldir: path to segmentation labels
    :param reg_data: path to csv file for regression"""

    exp_out_dir = os.path.join(checkpoint_dir, params.exp_name)
    os.makedirs(os.path.join(exp_out_dir, "regression_checkpoints"), exist_ok=True)
    os.makedirs(os.path.join(exp_out_dir, "segmentation_checkpoints"), exist_ok=True)

    reg_data = pd.read_csv(os.path.join(reg_data, "reg_co2_data.csv"))

    # create dataset
    data_train = create_dataset(
        datadir=datadir,
        seglabeldir=os.path.join(seglabeldir, "training/"),
        reg_data=reg_data,
        mult=1,
        train=True,
        channels=channels,
    )

    data_val = create_dataset(
        datadir=datadir,
        seglabeldir=os.path.join(seglabeldir, "validation/"),
        reg_data=reg_data,
        mult=1,
        channels=channels,
    )  # reg_data=reg_data

    # draw random subsamples
    train_sampler = RandomSampler(
        data_train, replacement=True, num_samples=int(2 * len(data_train) / 3)
    )

    # initialize data loaders
    train_dl = DataLoader(
        data_train,
        batch_size=params.bs,
        num_workers=6,
        pin_memory=True,
        sampler=train_sampler,
    )

    val_dl = DataLoader(data_val, batch_size=params.bs)

    best_mse, best_val_iou, best_val_acc = np.inf, 0.0, 0.0

    # define losses
    loss_r = nn.L1Loss()  # regression loss
    loss_c = nn.CrossEntropyLoss()  # classification loss
    loss_s = nn.BCEWithLogitsLoss()  # segmentation loss

    for epoch in range(params.ep):

        model.train()

        train_loss_total, train_acc_total, train_bin_acc_total = 0, 0, 0
        train_ious = []
        train_image_loss_total, train_gen_loss_total, train_bin_loss_total = 0, 0, 0

        progress = tqdm(enumerate(train_dl), desc="Train Loss: ", total=len(train_dl))
        for i, batch in progress:
            x = batch["img"].float().to(device)
            w = batch["weather"].float().to(device)
            y = batch["fpt"].float().to(device)
            e = batch["gen_output"].float().to(device)
            t = batch["type"].long().to(device)

            output, reg_output, logits = model(x, w)

            output_binary = np.zeros(output.shape)
            output_binary[output.cpu().detach().numpy() >= 0] = 1

            # derive IoU values
            for j in range(y.shape[0]):
                z = jaccard_score(
                    y[j].flatten().cpu().detach().numpy(), output_binary[j][0].flatten()
                )
                if (
                    np.sum(output_binary[j][0]) != 0
                    and np.sum(y[j].cpu().detach().numpy()) != 0
                ):
                    train_ious.append(z)

            bin_acc = multi_acc(logits, t)
            train_bin_acc_total += bin_acc

            # derive loss
            loss_image = loss_s(output, y.unsqueeze(dim=1))
            loss_gen = loss_r(reg_output, e.unsqueeze(dim=1))
            loss_bin = loss_c(logits, t)

            loss_epoch = (
                params.weight_segmentation * loss_image
                + params.weight_regression * loss_gen
                + params.weight_classification * loss_bin
            )

            train_image_loss_total += loss_image.item()
            train_gen_loss_total += loss_gen.item()
            train_bin_loss_total += loss_bin.item()

            train_loss_total += loss_epoch.item()
            progress.set_description(
                "Train Loss: {:.4f}".format(train_loss_total / (i + 1))
            )

            # learning
            opt.zero_grad()
            loss_epoch.backward()
            opt.step()

        torch.cuda.empty_cache()

        # evaluation
        model.eval()

        val_loss_total, val_acc_total, val_bin_acc_total = 0, 0, 0
        val_ious = []
        val_image_loss_total, val_gen_loss_total, val_bin_loss_total = 0, 0, 0

        progress = tqdm(enumerate(val_dl), desc="val Loss: ", total=len(val_dl))

        for j, batch in progress:
            x = batch["img"].float().to(device)
            w = batch["weather"].float().to(device)
            y = batch["fpt"].float().to(device)
            e = batch["gen_output"].float().to(device)
            t = batch["type"].long().to(device)

            output, reg_output, logits = model(x, w)

            bin_acc = multi_acc(logits, t)
            val_bin_acc_total += bin_acc

            # derive loss
            loss_image = loss_s(output, y.unsqueeze(dim=1))
            loss_bin = loss_c(logits, t)
            loss_gen = loss_r(reg_output, e.unsqueeze(dim=1))

            loss_epoch = (
                params.weight_segmentation * loss_image
                + params.weight_regression * loss_gen
                + params.weight_classification * loss_bin
            )

            val_loss_total += loss_epoch.item()
            val_image_loss_total += loss_image.item()
            val_bin_loss_total += loss_bin.item()
            val_gen_loss_total += loss_gen.item()

            # derive binary segmentation map from prediction
            output_binary = np.zeros(output.shape)
            output_binary[output.cpu().detach().numpy() >= 0] = 1

            # derive IoU values
            for k in range(y.shape[0]):
                z = jaccard_score(
                    y[k].flatten().cpu().detach().numpy(), output_binary[k][0].flatten()
                )
                if (
                    np.sum(output_binary[k][0]) != 0
                    and np.sum(y[k].cpu().detach().numpy()) != 0
                ):
                    val_ious.append(z)

            progress.set_description(
                "val Loss: {:.4f}".format(val_loss_total / (j + 1))
            )

        print(
            (
                "Epoch {:d}: total train loss={:.3f}, bce loss={:.3f}, mse loss={:.3f}, bin loss={:.3f}, total val loss={:.3f}, "
                " bce loss={:.3f}, mse loss={:.3f}, bin loss={:.3f}, train iou={:.3f}, val iou={:.3f}, train acc={:.3f}, val acc={:.3f}, bin train acc={:.3f}, bin val acc={:.3f}"
            ).format(
                epoch + 1,
                train_loss_total / (i + 1),
                train_image_loss_total / (i + 1),
                train_gen_loss_total / (i + 1),
                train_bin_loss_total / (i + 1),
                val_loss_total / (j + 1),
                val_image_loss_total / (j + 1),
                val_gen_loss_total / (j + 1),
                val_bin_loss_total / (j + 1),
                np.average(train_ious),
                np.average(val_ious),
                train_acc_total / (i + 1),
                val_acc_total / (j + 1),
                train_bin_acc_total / (i + 1),
                val_bin_acc_total / (j + 1),
            )
        )

        if np.average(val_ious) >= best_val_iou:
            best_val_iou = np.average(val_ious)
            # save model checkpoint
            torch.save(
                model.state_dict(),
                os.path.join(
                    exp_out_dir,
                    "segmentation_checkpoints/ep{:0d}_lr{:.0e}_bs{:02d}_mo{:.1f}_{:03d}.model".format(
                        params.ep, params.lr, params.bs, params.mo, epoch
                    ),
                ),
            )

        if val_gen_loss_total / (j + 1) <= best_mse:
            best_mse = val_gen_loss_total / (j + 1)
            # save model checkpoint
            torch.save(
                model.state_dict(),
                os.path.join(
                    exp_out_dir,
                    "regression_checkpoints/ep{:0d}_lr{:.0e}_bs{:02d}_mo{:.1f}_{:03d}.model".format(
                        params.ep, params.lr, params.bs, params.mo, epoch
                    ),
                ),
            )

        if val_bin_acc_total / (j + 1) >= best_val_acc:
            best_val_acc = val_bin_acc_total / (j + 1)
            torch.save(
                model.state_dict(),
                os.path.join(
                    exp_out_dir,
                    "classification_checkpoints/ep{:0d}_lr{:.0e}_bs{:02d}_mo{:.1f}_{:03d}.model".format(
                        params.ep, params.lr, params.bs, params.mo, epoch
                    ),
                ),
            )


def main():
    # setup argument parser
    parser = argparse.ArgumentParser()
    parser.add_argument("--ep", type=int, default=300, help="Number of epochs")
    parser.add_argument("--bs", type=int, nargs="?", default=32, help="Batch size")
    parser.add_argument(
        "--lr", type=float, nargs="?", default=0.7, help="Learning rate"
    )
    parser.add_argument("--mo", type=float, nargs="?", default=0.7, help="Momentum")
    parser.add_argument("--exp_name", type=str, default="", help="Name of experiment")
    parser.add_argument(
        "--channels", type=str, default="0,1,2,3,4,5,6,7,8,9,10,11", help="Channels"
    )
    parser.add_argument(
        "--weight_segmentation",
        type=float,
        default=1.0,
        help="Weight for segmentation loss",
    )
    parser.add_argument(
        "--weight_regression",
        type=float,
        default=1.0,
        help="Weight for regression loss",
    )
    parser.add_argument(
        "--weight_classification",
        type=float,
        default=1.0,
        help="Weight for classification loss",
    )
    parser.add_argument(
        "--data_dir", type=str, default="", help="Path to data directory"
    )
    parser.add_argument(
        "--seg_label_dir",
        type=str,
        default="",
        help="Path to segmentation label directory",
    )
    parser.add_argument(
        "--reg_dir", type=str, default="", help="Path to regression data directory"
    )
    parser.add_argument(
        "--checkpoint_dir", type=str, default="", help="Path to checkpoint directory"
    )

    args = parser.parse_args()

    channels = [int(c) for c in args.channels.split(",")]

    model = MultiTaskModel(n_channels=len(channels), n_classes=1)
    model.to(device)

    # initialize optimizer
    opt = optim.SGD(model.parameters(), lr=args.lr, momentum=args.mo)

    # run training
    train_model(
        model,
        args,
        opt,
        channels,
        datadir=args.data_dir,
        seglabeldir=args.seg_label_dir,
        reg_data=args.reg_data,
        checkpoint_dir=args.checkpoint_dir,
    )


if __name__ == "__main__":
    main()

from collections import OrderedDict
from typing import Any, List

import numpy as np
import torch
import wandb
from pytorch_lightning import LightningModule
from sklearn.metrics import confusion_matrix
from torchmetrics import MaxMetric, MeanMetric
from torchmetrics.classification.accuracy import Accuracy
from transformers import AutoFeatureExtractor, AutoModelForImageClassification

from models.components.swinv2_fusion_rgbd import SwinV2FusionRGBD
from models.components.swinv2_twin_rgbd import SwinV2TwinRGBD
from tllib.alignment.cdan import ConditionalDomainAdversarialLoss
from tllib.modules.domain_discriminator import DomainDiscriminator
from tllib.self_training.mcc import MinimumClassConfusionLoss


class SwinV2RGBDModule(LightningModule):
    """LightningModule for training ViT on RGBD data."""

    def __init__(
        self,
        model_name: str,
        optimizer: torch.optim.Optimizer,
        num_classes: int,
        scheduler: torch.optim.lr_scheduler = None,
        fine_tuning_checkpoint: str = None,
        cls_ckpt: str = None,
        cdan: bool = False,
        cdan_ddisc_in_feature: int = 768,
        cdan_ddisc_hidden_size: int = 1024,
        cdan_ddisc_entropy_conditioning: bool = False,
        mcc: bool = False,
        mcc_temperature: float = 1.0,
        extract_features_only: bool = False,
    ):
        super().__init__()

        # this line allows to access init params with 'self.hparams' attribute
        # also ensures init params will be stored in ckpt
        self.save_hyperparameters(logger=False)
        self.cdan = cdan
        self.mcc = mcc
        classifier = torch.nn.Linear(in_features=1024 * 2, out_features=num_classes, bias=True)
        if cls_ckpt:
            classifier.load_state_dict(torch.load(cls_ckpt)["model_rgbd_classifier_state_dict"])
        if model_name == "swinv2_twin_rgbd":
            self.net = SwinV2TwinRGBD(classifier=classifier)
        elif model_name == "swinv2_fusion_rgbd":
            self.net = SwinV2FusionRGBD(classifier=classifier)
        else:
            raise NotImplementedError("The given model name model is supported at the moment")

        if fine_tuning_checkpoint:
            weights = torch.load(fine_tuning_checkpoint)["state_dict"]
            weights_rn = OrderedDict()
            for layername in weights.keys():
                # Checkpoint layers are names with prepended "net.", which differs from vit layer names we get from "from_pretrained"
                if layername[:4] == "net.":
                    weights_rn[layername[4:]] = weights[layername]
            self.net.load_state_dict(weights_rn)
            model_name,

        self.criterion_classifier = torch.nn.CrossEntropyLoss()

        # metric objects for calculating and averaging accuracy across batches
        self.train_acc = Accuracy(task="multiclass", num_classes=num_classes)
        self.val_acc = Accuracy(task="multiclass", num_classes=num_classes)
        self.test_acc = Accuracy(task="multiclass", num_classes=num_classes)

        # for averaging loss across batches
        self.train_loss = MeanMetric()
        self.val_loss = MeanMetric()
        self.test_loss = MeanMetric()

        # for tracking best so far validation accuracy
        self.val_acc_best = MaxMetric()
        if cdan:
            self.ddisc = DomainDiscriminator(
                in_feature=cdan_ddisc_in_feature * num_classes,
                hidden_size=cdan_ddisc_hidden_size,
                sigmoid=False,
            )

            # loss function
            self.criterion_ddisc = ConditionalDomainAdversarialLoss(
                self.ddisc,
                entropy_conditioning=cdan_ddisc_entropy_conditioning,
                reduction="mean",
            )
            self.train_loss_classifier = MeanMetric()
            self.train_loss_ddisc = MeanMetric()

        if mcc:
            self.criterion_mcc = MinimumClassConfusionLoss(temperature=mcc_temperature)
            self.train_loss_mcc = MeanMetric()

    def forward(self, x: torch.Tensor):
        if self.hparams.extract_features_only:
            return (self.net(x)["features_rgb"], self.net(x)["features_depth"])
        x_rgb = x[:, :3]
        x_depth = x[:, 3]
        x_depth = torch.stack((x_depth, x_depth, x_depth), dim=1)
        outputs = self.net(x_rgb, x_depth)
        return outputs

    def on_train_start(self):
        # by default lightning executes validation step sanity checks before training starts,
        # so we need to make sure val_acc_best doesn't store accuracy from these checks
        self.val_acc_best.reset()

    def model_step(self, batch_src: Any):
        x, y = batch_src

        output_classifier = self.forward(x)
        logits_classifier = output_classifier["logits"]
        features_rgb_classifier = output_classifier["features_rgb"]
        features_depth_classifier = output_classifier["features_depth"]
        loss_classifier = self.criterion_classifier(logits_classifier, y)
        preds_classifier = torch.argmax(logits_classifier, dim=1)

        return (
            loss_classifier,
            preds_classifier,
            logits_classifier,
            features_rgb_classifier,
            features_depth_classifier,
            y,
        )

    def training_step(self, batch: Any, batch_idx: int):
        if not self.cdan and not self.mcc:
            batch_src = batch
        elif self.cdan or self.mcc:
            # NOTE: We expect a batch from MultiDataParallelLoader, which is a custom dataloader that
            #       collates data from multiple datasets in parallel
            #       In our use case this represents SOURCE and TARGET domain data for domain adaptation.
            batch_src, batch_target = batch
            x_target, y_target = batch_target
            target_net_output = self.forward(x_target)
            target_logits, target_rgb_features, target_depth_features = (
                target_net_output["logits"],
                target_net_output["features_rgb"],
                target_net_output["features_depth"],
            )

        loss_classifier, src_preds, src_logits, src_rgb_features, src_depth_features, src_targets = self.model_step(
            batch_src
        )
        loss = loss_classifier

        if self.cdan:
            src_features = torch.cat((src_rgb_features, src_depth_features), dim=1)
            target_features = torch.cat((target_rgb_features, target_depth_features), dim=1)

            self.train_loss_classifier(loss_classifier)
            self.log("train/loss_classifier", self.train_loss_classifier, on_step=False, on_epoch=True, prog_bar=True)
            loss_ddisc = self.criterion_ddisc(src_logits, src_features, target_logits, target_features)
            loss += loss_ddisc
            self.train_loss_ddisc(loss_ddisc)
            self.log("train/loss_ddisc", self.train_loss_ddisc, on_step=False, on_epoch=True, prog_bar=True)

        if self.mcc:
            loss_mcc = self.criterion_mcc(target_logits)
            loss += loss_mcc
            self.train_loss_mcc(loss_mcc)
            self.log("train/loss_mcc", self.train_loss_mcc, on_step=False, on_epoch=True, prog_bar=True)

        self.train_loss(loss)
        self.train_acc(src_preds, src_targets)
        self.log("train/loss", self.train_loss, on_step=False, on_epoch=True, prog_bar=True)
        self.log("train/acc", self.train_acc, on_step=False, on_epoch=True, prog_bar=True)

        # we can return here dict with any tensors
        # and then read it in some callback or in `training_epoch_end()` below
        # remember to always return loss from `training_step()` or backpropagation will fail!
        return {
            "loss": loss,
            "preds": src_preds,
            "targets": src_targets,
        }

    def on_training_epoch_end(self):
        # `outputs` is a list of dicts returned from `training_step()`

        # Warning: when overriding `training_epoch_end()`, lightning accumulates outputs from all batches of the epoch
        # this may not be an issue when training on mnist
        # but on larger datasets/models it's easy to run into out-of-memory errors

        # consider detaching tensors before returning them from `training_step()`
        # or using `on_train_epoch_end()` instead which doesn't accumulate outputs

        pass

    def validation_step(self, batch: Any, batch_idx: int):
        loss, preds, logits, features_rgb, features_depth, targets = self.model_step(batch)

        # update and log metrics
        self.val_loss(loss)
        self.val_acc(preds, targets)
        self.log("val/loss", self.val_loss, on_step=False, on_epoch=True, prog_bar=True)
        self.log("val/acc", self.val_acc, on_step=False, on_epoch=True, prog_bar=True)

        return {"loss": loss, "preds": preds, "targets": targets}

    def on_validation_epoch_end(self):
        acc = self.val_acc.compute()  # get current val acc
        self.val_acc_best(acc)  # update best so far val acc
        # log `val_acc_best` as a value through `.compute()` method, instead of as a metric object
        # otherwise metric would be reset by lightning after each epoch
        self.log("val/acc_best", self.val_acc_best.compute(), prog_bar=True)

    def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: int = 0) -> Any:
        x, y = batch
        output_classifier = self.forward(x)
        logits_classifier = output_classifier["logits"]
        features_classifier = output_classifier["hidden_states"][-1][:, 0, :]
        preds_classifier = torch.argmax(logits_classifier, dim=1)

        return {
            "preds": preds_classifier,
            "targets": y,
            "logits": logits_classifier,
            "features": features_classifier,
        }

    def on_test_epoch_start(self):
        self.preds_test_all = None
        self.targets_test_all = None
        self.cls_tokens_all = None

    def test_step(self, batch: Any, batch_idx: int):
        loss, preds, logits, features_rgb, features_depth, targets = self.model_step(batch)

        # update and log metrics
        self.test_loss(loss)
        self.test_acc(preds, targets)
        self.log("test/loss", self.test_loss, on_step=False, on_epoch=True, prog_bar=True)
        self.log("test/acc", self.test_acc, on_step=False, on_epoch=True, prog_bar=True)

        self.preds_test_all = (
            torch.cat((self.preds_test_all, preds), 0) if torch.is_tensor(self.preds_test_all) else preds
        )
        self.targets_test_all = (
            torch.cat((self.targets_test_all, targets), 0) if torch.is_tensor(self.targets_test_all) else targets
        )
        # self.cls_tokens_all = (
        #     torch.cat((self.cls_tokens_all, features), 0) if torch.is_tensor(self.cls_tokens_all) else features
        # )

        return {"loss": loss, "preds": preds, "targets": targets}

    def on_test_epoch_end(self):
        class_names = list(self.trainer.datamodule.label2idx.keys())
        cm = confusion_matrix(
            y_true=self.targets_test_all.cpu(),
            y_pred=self.preds_test_all.cpu(),
            normalize="true",
        )
        class_acc = cm.diagonal()
        data = [[name, acc] for (name, acc) in zip(class_names, class_acc)]
        table = wandb.Table(data=data, columns=["class_name", "acc"])

        # Accuracy Per class Barchart
        self.logger.experiment.log(
            {
                "test/acc_per_class": wandb.plot.bar(
                    table,
                    "class_name",
                    "acc",
                    title="Per Class Accuracy",
                )
            }
        )
        # Confusion Matrix (normalized on trues)
        self.logger.experiment.log({"conf_mat" : wandb.plot.confusion_matrix(probs=None, y_true=self.targets_test_all.cpu().numpy(), preds=self.preds_test_all.cpu().numpy(), class_names=class_names)})
        # self.logger.experiment.log(
        #     {
        #         "test/confmat": wandb.sklearn.plot_confusion_matrix(
        #             y_true=self.targets_test_all.cpu(),
        #             y_pred=self.preds_test_all.cpu(),
        #             labels=class_names,
        #             normalize="true",
        #         )
        #     }
        # )
        # # TSNE // Embedding projector
        # tsne_cols = np.arange(self.cls_tokens_all.size(dim=1)).astype(str).tolist()
        # tsne_cols.insert(0, "target")

        # tsne_embeddings = self.cls_tokens_all.cpu().tolist()
        # tsne_targets = [self.trainer.datamodule.idx2label[cls_idx] for cls_idx in self.targets_test_all.cpu().tolist()]
        # tsne_data = [[target] + tsne_embeddings[i] for i, target in enumerate(tsne_targets)]

        # self.logger.experiment.log(
        #     {
        #         "embeddings": wandb.Table(
        #             columns=tsne_cols,
        #             data=tsne_data,
        #         )
        #     }
        # )

    def configure_optimizers(self):
        """Choose what optimizers and learning-rate schedulers to use in your optimization. Normally you'd need one. But
        in the case of GANs or similar you might have multiple.

        Examples:
            https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#configure-optimizers
        """
        optimizer = self.hparams.optimizer(params=self.parameters())
        if self.hparams.scheduler is not None:
            scheduler = self.hparams.scheduler(optimizer=optimizer)
            return {
                "optimizer": optimizer,
                "lr_scheduler": {
                    "scheduler": scheduler,
                    "monitor": "val/loss",
                    "interval": "epoch",
                    "frequency": 1,
                },
            }
        return {"optimizer": optimizer}

    def on_load_checkpoint(self, checkpoint) -> None:
        print("Checkpoint Loaded")
        return super().on_load_checkpoint(checkpoint)


if __name__ == "__main__":
    _ = SwinV2RGBDModule(None, None, None, None)

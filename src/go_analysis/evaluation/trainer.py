"""evaluation/trainer.py — 模型训练循环。

注意: GoStrengthModel.forward 接口为 (x, env_vec, mask)
  x:       (B, T, 12) 特征序列
  env_vec: (B, env_dim) 环境向量 (或 None)
  mask:    (B, T) 填充掩码 (True=pad)
  输出:    (B, n_classes) 段位概率分布
"""
import time
import logging
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, random_split

from .model import GoStrengthModel
from .dataset import GoDataset, collate_padded

log = logging.getLogger("trainer")


class Trainer:
    """GoStrengthModel 训练器。"""

    def __init__(self, model: GoStrengthModel,
                 train_dataset: Dataset,
                 val_dataset: Optional[Dataset] = None,
                 lr: float = 1e-4,
                 weight_decay: float = 1e-5,
                 device: str = "auto"):
        self.model = model
        self.train_dataset = train_dataset
        self.val_dataset = val_dataset
        self.lr = lr
        self.weight_decay = weight_decay

        if device == "auto":
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = device

        self.model = self.model.to(self.device)
        self.optimizer = optim.AdamW(
            self.model.parameters(), lr=lr, weight_decay=weight_decay
        )
        self.criterion = nn.CrossEntropyLoss()

    def _prepare_batch(self, batch: dict):
        """将 batch 数据移到设备并准备模型输入。"""
        feats = batch["features"].to(self.device)        # (B, T, 12)
        seq_lens = batch["seq_lens"]                     # (B,)
        
        # 创建 mask: True=pad
        B, T, _ = feats.shape
        mask = torch.arange(T, device=self.device).unsqueeze(0) >= \
               seq_lens.unsqueeze(1).to(self.device)     # (B, T)

        # 创建环境向量 (使用全游戏特征的统计)
        env_vec = torch.zeros(B, self.model.env_dim, device=self.device)

        return feats, env_vec, mask

    def train_epoch(self, dataloader: DataLoader) -> float:
        self.model.train()
        total_loss = 0.0
        for batch in dataloader:
            feats, env_vec, mask = self._prepare_batch(batch)
            
            # 目标: 取黑白段位的平均值作为输出目标
            br = batch["black_rank"].to(self.device)
            wr = batch["white_rank"].to(self.device)
            targets = br  # 目前只预测黑方段位

            self.optimizer.zero_grad()
            logits = self.model(feats, env_vec, mask)
            loss = self.criterion(logits, targets)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
            self.optimizer.step()

            total_loss += loss.item()

        return total_loss / len(dataloader)

    def evaluate(self, dataloader: DataLoader) -> dict:
        self.model.eval()
        total_loss = 0.0
        correct = 0
        total = 0

        with torch.no_grad():
            for batch in dataloader:
                feats, env_vec, mask = self._prepare_batch(batch)
                br = batch["black_rank"].to(self.device)
                targets = br

                logits = self.model(feats, env_vec, mask)
                loss = self.criterion(logits, targets)
                total_loss += loss.item()

                pred = logits.argmax(dim=1)
                correct += (pred == targets).sum().item()
                total += targets.size(0)

        return {
            "loss": total_loss / len(dataloader),
            "accuracy": correct / max(total, 1),
            "correct": correct,
            "total": total,
        }

    def train(self, epochs: int = 50, batch_size: int = 32,
              val_split: float = 0.1, patience: int = 10,
              checkpoint_dir: Optional[str] = None,
              log_interval: int = 5) -> dict:
        """完整训练流程。"""
        if self.val_dataset is None:
            val_size = max(1, int(len(self.train_dataset) * val_split))
            train_size = len(self.train_dataset) - val_size
            train_ds, val_ds = random_split(self.train_dataset, [train_size, val_size])
        else:
            train_ds = self.train_dataset
            val_ds = self.val_dataset

        train_loader = DataLoader(
            train_ds, batch_size=batch_size, shuffle=True,
            collate_fn=collate_padded, num_workers=0,
        )
        val_loader = DataLoader(
            val_ds, batch_size=batch_size, shuffle=False,
            collate_fn=collate_padded, num_workers=0,
        )

        best_val_loss = float("inf")
        best_epoch = 0
        no_improve = 0
        history = {"train_loss": [], "val_loss": [], "val_acc": []}

        if checkpoint_dir:
            Path(checkpoint_dir).mkdir(parents=True, exist_ok=True)

        for epoch in range(1, epochs + 1):
            t0 = time.time()
            train_loss = self.train_epoch(train_loader)
            val_metrics = self.evaluate(val_loader)
            dt = time.time() - t0

            history["train_loss"].append(train_loss)
            history["val_loss"].append(val_metrics["loss"])
            history["val_acc"].append(val_metrics["accuracy"])

            if val_metrics["loss"] < best_val_loss:
                best_val_loss = val_metrics["loss"]
                best_epoch = epoch
                no_improve = 0
                if checkpoint_dir:
                    ckpt_path = Path(checkpoint_dir) / "best.pt"
                    torch.save(self.model.state_dict(), ckpt_path)
                    log.info(f"✓ Checkpoint: {ckpt_path}")
            else:
                no_improve += 1

            if epoch % log_interval == 0 or epoch == 1:
                log.info(
                    f"Epoch {epoch:3d}/{epochs}  loss={train_loss:.4f}  "
                    f"val_loss={val_metrics['loss']:.4f}  "
                    f"acc={val_metrics['accuracy']:.3f}  ({dt:.1f}s)"
                )

            if no_improve >= patience:
                log.info(f"⏹ Early stopping at epoch {epoch}")
                break

        return {
            "best_epoch": best_epoch,
            "best_val_loss": best_val_loss,
            "history": history,
            "epochs_trained": epoch,
        }

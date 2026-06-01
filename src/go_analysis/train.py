"""
Training script for Go strength Transformer model.

Supports v1 (shared transformer) and v2 (BW-separated causal cross-attention).

Usage:
    # From analysis store (.npz directory):
    python -m go_analysis.train \
        --store analysis_store \
        --n-train 20 --n-test 5 \
        --epochs 50

    # From pre-computed analysis JSON:
    python -m go_analysis.train \
        --data analysis_results.json \
        --epochs 50
"""
import argparse
import json
import os
import random

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, random_split
from tqdm import tqdm

from .dataset import GoDataset, collate_padded
from .model import GoStrengthModel as V2Model
from .model_v1_archive import GoStrengthModel as V1Model
from .analysis_format import AnalysisStore


def set_seed(seed: int = 42):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def compute_accuracy(probs, labels):
    return (probs.argmax(dim=1) == labels).float().mean().item()


def compute_qwk(probs, labels, n_classes=5):
    preds = probs.argmax(dim=1).cpu().numpy()
    true = labels.cpu().numpy()
    cm = np.zeros((n_classes, n_classes), dtype=np.float64)
    for t, p in zip(true, preds):
        cm[t, p] += 1
    row_sum, col_sum = cm.sum(axis=1), cm.sum(axis=0)
    total = cm.sum()
    expected = np.outer(row_sum, col_sum) / total
    w = np.zeros((n_classes, n_classes), dtype=np.float64)
    for i in range(n_classes):
        for j in range(n_classes):
            w[i, j] = (i - j) ** 2 / (n_classes - 1) ** 2
    obs, exp = (w * cm).sum(), (w * expected).sum()
    return 1.0 - obs / exp if exp != 0 else 0.0


class NPZDataset(Dataset):
    """Variable-length dataset from AnalysisStore (.npz files).
    
    Each item: (features, mask, env_vec, label)
    - features: (T, 12) — variable-length
    - mask: (T,) — all False (no padding at item level)
    - env_vec: (12,) — global stats
    - label: int
    """

    def __init__(self, store_dir, game_ids, label_map=None, model_version="v2"):
        self.store = AnalysisStore(store_dir)
        self.game_ids = game_ids
        self.label_map = label_map or {"4d": 0, "5d": 1}
        self.model_version = model_version
        self.data = self._load()

    def _load(self):
        data = []
        for gid in self.game_ids:
            rec = self.store.get(gid)
            if rec is None or rec.features.shape[0] < 10:
                continue
            rank = (rec.game.rank_black or rec.game.rank_white or "").strip()
            label = self.label_map.get(rank, 0)

            feats = rec.get_features_f32()   # (T, 12)
            ctx = rec.get_global_stats_f32()  # (12,)
            data.append((feats, ctx, label, gid))
        print(f"  Loaded {len(data)} games from store")
        return data

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        feats, ctx, label, _ = self.data[idx]
        T = len(feats)
        return (
            torch.from_numpy(feats),       # (T, 12)
            torch.zeros(T, dtype=torch.bool),  # mask = no pad
            torch.from_numpy(ctx),          # (12,)
            torch.tensor(label, dtype=torch.long),
        )


def collate_varlen(batch):
    """Dynamic-padding collate for variable-length batches."""
    seqs, masks, ctxs, labels = zip(*batch)
    max_t = max(s.size(0) for s in seqs)
    B = len(seqs)
    D = seqs[0].size(1)

    padded = torch.zeros(B, max_t, D)
    batch_mask = torch.ones(B, max_t, dtype=torch.bool)
    for i, (s, m) in enumerate(zip(seqs, masks)):
        t = s.size(0)
        padded[i, :t] = s
        batch_mask[i, :t] = m  # all False (no pad data)

    return padded, batch_mask, torch.stack(ctxs), torch.stack(labels)


def train_model(
    model_version="v2",
    store_dir=None,
    n_train=20,
    n_test=5,
    data_path=None,
    analysis_list=None,
    label_map=None,
    n_classes=5,
    input_dim=12,
    d_model=128,
    nhead=8,
    num_layers=2,
    max_seq_len=400,
    batch_size=8,
    epochs=50,
    lr=1e-3,
    weight_decay=1e-5,
    val_split=0.2,
    save_dir="checkpoints",
    device="auto",
    seed=42,
):
    set_seed(seed)
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    device = torch.device(device)
    print(f"[Train] Device: {device}")

    # ── 数据加载 ──
    if store_dir:
        # 从 AnalysisStore 加载
        store = AnalysisStore(store_dir)
        all_ids = store.list_games()
        random.seed(seed)
        random.shuffle(all_ids)

        total = n_train + n_test
        selected = all_ids[:total]
        train_ids = selected[:n_train]
        test_ids = selected[n_train:total]
        print(f"[Train] Store: {len(all_ids)} games, using {n_train}+{n_test}")

        train_ds = NPZDataset(store_dir, train_ids, label_map, model_version)
        test_ds = NPZDataset(store_dir, test_ids, label_map, model_version)
        train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True,
                                  collate_fn=collate_varlen)
        test_loader = DataLoader(test_ds, batch_size=batch_size, shuffle=False,
                                  collate_fn=collate_varlen)

        print(f"  Train: {len(train_ds)}, Test: {len(test_ds)}")

    elif data_path or analysis_list:
        # 旧方式: JSON → GoDataset
        dataset = GoDataset(
            data_path=data_path,
            analysis_list=analysis_list,
            label_map=label_map,
            max_seq_len=max_seq_len,
        )
        val_size = int(len(dataset) * val_split)
        train_size = len(dataset) - val_size
        train_ds, test_ds = random_split(dataset, [train_size, val_size])
        train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True,
                                  collate_fn=collate_padded)
        test_loader = DataLoader(test_ds, batch_size=batch_size, shuffle=False,
                                  collate_fn=collate_padded)
        print(f"[Train] Train: {train_size}, Val: {test_size}")
    else:
        raise ValueError("Must provide store_dir or data_path/analysis_list")

    # ── 模型 ──
    if model_version == "v1":
        ModelClass = V1Model
        model_kwargs = dict(
            input_dim=input_dim, d_model=d_model, nhead=nhead,
            num_layers=4, n_classes=n_classes, dropout=0.1,
        )
    else:
        ModelClass = V2Model
        model_kwargs = dict(
            input_dim=input_dim, d_model=d_model, nhead=nhead,
            num_layers=num_layers, n_classes=n_classes,
            env_dim=12, dropout=0.1,
        )

    model = ModelClass(**model_kwargs).to(device)
    total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"[Train] Model: v{model_version}, params: {total_params:,}")

    # ── 优化 ──
    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    # ── 训练循环 ──
    best_qwk = 0.0
    os.makedirs(save_dir, exist_ok=True)

    for epoch in range(1, epochs + 1):
        model.train()
        train_loss = 0.0
        train_acc = 0.0
        n_batches = 0

        pbar = tqdm(train_loader, desc=f"Epoch {epoch}/{epochs}")
        for seqs, masks, ctx, labels in pbar:
            seqs = seqs.to(device)
            masks = masks.to(device)
            ctx = ctx.to(device)
            labels = labels.to(device)

            optimizer.zero_grad()
            probs, theta = model.forward_details(seqs, ctx, mask=masks)
            loss = model.output_head.get_ordinal_loss(theta, labels)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()

            acc = compute_accuracy(probs, labels)
            train_loss += loss.item()
            train_acc += acc
            n_batches += 1
            pbar.set_postfix({"loss": f"{loss.item():.4f}", "acc": f"{acc:.3f}"})

        # Validation
        model.eval()
        val_loss = 0.0
        val_acc = 0.0
        all_probs, all_labels = [], []

        with torch.no_grad():
            for seqs, masks, ctx, labels in test_loader:
                seqs = seqs.to(device)
                masks = masks.to(device)
                ctx = ctx.to(device)
                labels = labels.to(device)

                probs, theta = model.forward_details(seqs, ctx, mask=masks)
                loss = model.output_head.get_ordinal_loss(theta, labels)

                val_loss += loss.item()
                val_acc += compute_accuracy(probs, labels)
                all_probs.append(probs.cpu())
                all_labels.append(labels.cpu())

        all_probs = torch.cat(all_probs, dim=0)
        all_labels = torch.cat(all_labels, dim=0)
        qwk = compute_qwk(all_probs, all_labels, n_classes)
        scheduler.step()

        avg_train_loss = train_loss / n_batches
        avg_train_acc = train_acc / n_batches
        avg_val_loss = val_loss / len(test_loader)
        avg_val_acc = val_acc / len(test_loader)

        print(
            f"  [Epoch {epoch:3d}] "
            f"Train loss: {avg_train_loss:.4f} acc: {avg_train_acc:.3f} | "
            f"Val loss: {avg_val_loss:.4f} acc: {avg_val_acc:.3f} QWK: {qwk:.4f}"
        )

        if qwk > best_qwk:
            best_qwk = qwk
            ckpt = {
                "epoch": epoch,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "qwk": qwk,
                "model_version": model_version,
                "model_kwargs": model_kwargs,
            }
            ckpt_path = os.path.join(save_dir, f"v{model_version}_best.pt")
            torch.save(ckpt, ckpt_path)
            print(f"  → Saved best model (QWK={qwk:.4f}) to {ckpt_path}")

        # 每 epoch 保存
        torch.save({
            "epoch": epoch,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
        }, os.path.join(save_dir, f"v{model_version}_latest.pt"))

    print(f"[Train] Done. Best QWK: {best_qwk:.4f}")

    # ── 逐局测试结果 ──
    if len(test_ds) <= 10:
        print(f"\n  {'='*45}")
        print(f"  {'Game':<30} {'Label':>5} {'Pred':>5} {'OK?':>5}")
        print(f"  {'-'*30} {'-'*5} {'-'*5} {'-'*5}")
        rank_map = {v: k for k, v in (label_map or {"4d":0, "5d":1}).items()}
        model.eval()
        correct = 0
        for i in range(len(test_ds)):
            feats, _, ctx, label = test_ds[i]
            x = feats.unsqueeze(0).to(device)
            v = ctx.unsqueeze(0).to(device)
            m = torch.zeros(1, feats.size(0), dtype=torch.bool).to(device)
            with torch.no_grad():
                probs = model(x, v, mask=m)
                pred = probs.argmax(1).item()
            ok = "✓" if label == pred else "✗"
            if label == pred:
                correct += 1
            gid = test_ds.data[i][3][:28]
            print(f"  {gid:30} {rank_map.get(label,str(label)):>5} "
                  f"{rank_map.get(pred,str(pred)):>5} {ok:>5}")
        print(f"\n  Test Accuracy: {correct}/{len(test_ds)} ({100*correct/len(test_ds):.0f}%)")

    return model, best_qwk


def main():
    parser = argparse.ArgumentParser(description="Train Go strength model")
    parser.add_argument("--model", choices=["v1", "v2"], default="v2",
                        help="Model version")
    parser.add_argument("--store", type=str, default=None,
                        help="AnalysisStore directory (.npz files)")
    parser.add_argument("--data", type=str, default=None,
                        help="Path to analysis JSON (legacy)")
    parser.add_argument("--n-train", type=int, default=20)
    parser.add_argument("--n-test", type=int, default=5)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--d-model", type=int, default=128)
    parser.add_argument("--nhead", type=int, default=8)
    parser.add_argument("--num-layers", type=int, default=2)
    parser.add_argument("--n-classes", type=int, default=5)
    parser.add_argument("--save-dir", type=str, default="checkpoints")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    train_model(
        model_version=args.model,
        store_dir=args.store,
        n_train=args.n_train,
        n_test=args.n_test,
        data_path=args.data,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        d_model=args.d_model,
        nhead=args.nhead,
        num_layers=args.num_layers,
        n_classes=args.n_classes,
        save_dir=args.save_dir,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()

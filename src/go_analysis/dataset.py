"""
PyTorch Dataset for Go game strength analysis.

Converts structured analysis JSON into padded tensor batches.
"""

import json
from typing import Optional

import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm

from .models import (
    GoMoveData,
    GoGameData,
    extract_features_from_analysis,
    compute_global_stats,
)


class GoDataset(Dataset):
    """Dataset of Go games for strength prediction.

    Each item is a tuple (move_seq, mask, global_stats, label).
    - move_seq:  (T, 12)  variable-length feature sequence (T = actual moves)
    - mask:      (T,)     all False (no padding at item level)
    - global_stats: (12,) game-level aggregate features
    - label:     int      ordinal strength label (0..N-1)
    """

    def __init__(
        self,
        data_path: Optional[str] = None,
        analysis_list: Optional[list] = None,
        label_map: Optional[dict] = None,
        max_seq_len: int = 400,
        min_moves: int = 20,
    ):
        """
        Parameters
        ----------
        data_path : str, optional
            Path to a JSON file with per-game analyses. Each entry must have:
                "sgf_path": str, "analysis": dict (KataGo output), "label": int
            If provided, loads from file. Otherwise uses analysis_list.
        analysis_list : list, optional
            List of dicts with the same structure (used if data_path is None).
        label_map : dict, optional
            Mapping from original label to ordinal 0..N-1.
            e.g. {"3d": 0, "4d": 1, "5d": 2, "6d": 3, "7d": 4}
            If None, labels are used as-is.
        max_seq_len : int
            Maximum sequence length. Longer games are truncated.
        min_moves : int
            Minimum number of moves per player. Shorter games are skipped.
        """
        self.max_seq_len = max_seq_len
        self.min_moves = min_moves
        self.label_map = label_map

        # Load data
        if data_path is not None:
            with open(data_path, "r") as f:
                raw_data = json.load(f)
        elif analysis_list is not None:
            raw_data = analysis_list
        else:
            raise ValueError("Either data_path or analysis_list must be provided.")

        # Convert to GoGameData list
        self.games: list = []
        self._build(raw_data)

        print(f"[Dataset] Loaded {len(self.games)} games (min {min_moves} moves/player)")

    def _build(self, raw_data: list):
        for entry in tqdm(raw_data, desc="Building dataset"):
            sgf_path = entry.get("sgf_path", "")
            analysis = entry.get("analysis", {})
            label = entry.get("label", 0)

            if self.label_map is not None:
                label = self.label_map.get(str(label), 0)

            # Extract per-move features for both players
            moves_b = extract_features_from_analysis(analysis, "B")
            moves_w = extract_features_from_analysis(analysis, "W")

            for moves, player in [(moves_b, 0), (moves_w, 1)]:
                if len(moves) < self.min_moves:
                    continue

                # Apply label to each move
                for m in moves:
                    m.label = label

                global_stats = compute_global_stats(moves)

                game_data = GoGameData(
                    game_id=sgf_path,
                    moves=moves,
                    global_stats=global_stats,
                    label=label,
                )
                self.games.append((moves, global_stats, label))

    def __len__(self):
        return len(self.games)

    def __getitem__(self, idx):
        moves, global_stats, label = self.games[idx]
        seq_len = min(len(moves), self.max_seq_len)
        feats = np.stack([m.features for m in moves[:seq_len]], axis=0)

        T = feats.shape[0]
        # 变长: 返回原始长度, collate_fn 里动态 pad
        return (
            torch.from_numpy(feats),        # (T, 12) — T = 实际手数
            torch.zeros(T, dtype=torch.bool),  # (T,) — 无 pad, mask = False
            torch.from_numpy(global_stats),  # (12,)
            torch.tensor(label, dtype=torch.long),  # scalar
        )


def collate_padded(batch):
    """Collate with dynamic padding per batch.

    Pads all sequences to the longest in this batch, avoiding global
    fixed-length padding waste.
    """
    seqs, masks, globals_, labels = zip(*batch)

    # Find max length in this batch
    max_t = max(s.size(0) for s in seqs)
    B = len(seqs)
    feat_dim = seqs[0].size(1)

    # Build padded tensor + mask
    padded = torch.zeros(B, max_t, feat_dim, dtype=seqs[0].dtype)
    batch_mask = torch.ones(B, max_t, dtype=torch.bool)  # True = pad

    for i, (s, m) in enumerate(zip(seqs, masks)):
        t = s.size(0)
        padded[i, :t] = s
        batch_mask[i, :t] = m  # copy original mask (all False for no-pad items)

    return (
        padded,           # (B, max_t, 12)
        batch_mask,       # (B, max_t)  True=pad
        torch.stack(globals_, dim=0),  # (B, 12)
        torch.stack(labels, dim=0),    # (B,)
    )

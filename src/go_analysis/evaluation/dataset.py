"""evaluation/dataset.py — GoDataset for strength prediction training.

从 BaseStore 加载 AnalysisRecord 数据，提供 PyTorch Dataset 接口。
"""
import json
from typing import Optional
import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader

from ..data.store import BaseStore, NpzStore
from ..data.format import AnalysisRecord


class GoDataset(Dataset):
    """围棋段位预测数据集。

    从 store 中加载 AnalysisRecord，提取 features 和段位标签。
    """

    def __init__(self, store: BaseStore, game_ids: list[str] = None,
                 max_seq_len: int = 512):
        self.store = store
        self.max_seq_len = max_seq_len
        self._records: list[dict] = []

        if game_ids is None:
            game_ids = store.list()

        for gid in game_ids:
            record = store.load(gid)
            if record is None or record.num_moves == 0:
                continue
            # 需要至少有一个段位标签
            br = record.metadata.black_rank
            wr = record.metadata.white_rank
            if br is None and wr is None:
                continue
            self._records.append({
                "game_id": gid,
                "features": record.features,
                "black_rank": br or 0,
                "white_rank": wr or 0,
            })

    def __len__(self) -> int:
        return len(self._records)

    def __getitem__(self, idx: int) -> dict:
        rec = self._records[idx]
        feats = rec["features"][:self.max_seq_len]
        return {
            "game_id": rec["game_id"],
            "features": torch.from_numpy(feats).float(),
            "black_rank": torch.tensor(rec["black_rank"], dtype=torch.long),
            "white_rank": torch.tensor(rec["white_rank"], dtype=torch.long),
            "seq_len": min(len(feats), self.max_seq_len),
        }


def collate_padded(batch: list[dict]) -> dict:
    """将变长序列填充到相同长度。"""
    max_len = max(item["seq_len"] for item in batch)
    batch_feats = []
    batch_br = []
    batch_wr = []
    batch_lens = []
    batch_ids = []

    for item in batch:
        seq_len = item["seq_len"]
        feats = item["features"]
        if seq_len < max_len:
            pad = torch.zeros(max_len - seq_len, feats.size(1), dtype=feats.dtype)
            feats = torch.cat([feats, pad], dim=0)
        batch_feats.append(feats)
        batch_br.append(item["black_rank"])
        batch_wr.append(item["white_rank"])
        batch_lens.append(seq_len)
        batch_ids.append(item["game_id"])

    return {
        "game_ids": batch_ids,
        "features": torch.stack(batch_feats, dim=0),   # (B, T, 12)
        "black_rank": torch.stack(batch_br, dim=0),
        "white_rank": torch.stack(batch_wr, dim=0),
        "seq_lens": torch.tensor(batch_lens, dtype=torch.long),
    }

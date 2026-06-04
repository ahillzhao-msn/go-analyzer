"""NpzStore — 文件系统 NPZ 存储后端。"""
import json
import os
from pathlib import Path
from typing import Optional

import numpy as np

from .base import BaseStore
from ..format import AnalysisRecord


class NpzStore(BaseStore):
    """以 .npz 文件形式存储分析结果到本地文件系统。

    文件约定:
      {store_dir}/{game_id}.npz — 完整分析结果

    使用 np.savez / np.load 格式，跨平台兼容。
    """

    def __init__(self, store_dir: str, readonly: bool = False):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.readonly = readonly

    def _path(self, game_id: str) -> Path:
        return self.store_dir / f"{game_id}.npz"

    def save(self, game_id: str, record: AnalysisRecord) -> str:
        if self.readonly:
            raise PermissionError(f"Store is read-only: {self.store_dir}")
        path = self._path(game_id)
        d = record.to_npz_dict()
        np.savez(str(path), **d)
        return str(path)

    def load(self, game_id: str) -> Optional[AnalysisRecord]:
        path = self._path(game_id)
        if not path.exists():
            return None
        try:
            data = np.load(str(path), allow_pickle=True)
            return AnalysisRecord.from_npz(data)
        except Exception:
            return None

    def list(self) -> list[str]:
        return sorted({
            f.stem
            for f in self.store_dir.glob("*.npz")
            if f.is_file() and not f.stem.endswith(".meta")
        })

    def exists(self, game_id: str) -> bool:
        return self._path(game_id).exists()

    def count(self) -> int:
        return len(list(self.store_dir.glob("*.npz")))

    def remove(self, game_id: str) -> bool:
        path = self._path(game_id)
        if path.exists():
            path.unlink()
            return True
        return False

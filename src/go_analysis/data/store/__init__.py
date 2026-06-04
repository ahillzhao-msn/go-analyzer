"""data/store — 分析结果存储抽象。"""
from .base import BaseStore
from .npz_store import NpzStore
from .registry import StoreRegistry

__all__ = ["BaseStore", "NpzStore", "StoreRegistry"]

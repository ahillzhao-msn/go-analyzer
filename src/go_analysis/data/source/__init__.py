"""data/source — 棋谱来源抽象。"""
from .base import BaseSource
from .folder import FolderSource
from .registry import SourceRegistry

__all__ = ["BaseSource", "FolderSource", "SourceRegistry"]

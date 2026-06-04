"""BaseStore — 分析结果存储的抽象接口。"""
from abc import ABC, abstractmethod
from typing import Optional

from ..format import AnalysisRecord


class BaseStore(ABC):
    """存储抽象基类。

    所有存储实现 (NPZ文件/SQL/远程) 必须继承此类。
    核心方法: save() → load() → list() → exists()
    """

    @abstractmethod
    def save(self, game_id: str, record: AnalysisRecord) -> str:
        """保存分析结果，返回存储路径/标识。"""
        ...

    @abstractmethod
    def load(self, game_id: str) -> Optional[AnalysisRecord]:
        """读取分析结果，不存在返回 None。"""
        ...

    @abstractmethod
    def list(self) -> list[str]:
        """返回所有已保存的 game_id 列表。"""
        ...

    @abstractmethod
    def exists(self, game_id: str) -> bool:
        """检查分析结果是否存在。"""
        ...

    @abstractmethod
    def count(self) -> int:
        """已保存的结果总数。"""
        ...

    @abstractmethod
    def remove(self, game_id: str) -> bool:
        """删除指定结果。"""
        ...

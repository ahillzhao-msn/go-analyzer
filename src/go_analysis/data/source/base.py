"""BaseSource — 棋谱来源的抽象接口。"""
from abc import ABC, abstractmethod
from typing import Optional


class BaseSource(ABC):
    """棋谱来源抽象基类。

    所有来源实现 (文件夹/数据库/API) 必须继承此类。
    核心方法：list_games() → get_game() → count()
    """

    @abstractmethod
    def list_games(self) -> list[str]:
        """返回所有可用棋谱的 game_id 列表。"""
        ...

    @abstractmethod
    def get_game(self, game_id: str) -> tuple[str, dict]:
        """获取指定棋谱的 SGF 内容和元数据。

        Returns:
            (sgf_content: str, metadata: dict)
        """
        ...

    @abstractmethod
    def count(self) -> int:
        """棋谱总数。"""
        ...

    @abstractmethod
    def exists(self, game_id: str) -> bool:
        """检查指定棋谱是否存在。"""
        ...

    def get_metadata(self, game_id: str) -> dict:
        """仅获取元数据（如来源框架已缓存元数据可重写此方法）。"""
        _, meta = self.get_game(game_id)
        return meta

    def close(self):
        """释放资源（需要时重写）。"""
        pass

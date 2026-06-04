"""SourceRegistry — 棋谱来源的工厂+注册表。"""
from .base import BaseSource
from .folder import FolderSource


class SourceRegistry:
    """管理所有已注册的 SGF 来源类型。"""

    _sources: dict[str, type[BaseSource]] = {}

    @classmethod
    def register(cls, name: str, source_cls: type[BaseSource]):
        """注册一个新的来源类型。"""
        cls._sources[name] = source_cls

    @classmethod
    def create(cls, name: str, **kwargs) -> BaseSource:
        """根据名称创建来源实例。"""
        source_cls = cls._sources.get(name)
        if source_cls is None:
            raise ValueError(
                f"Unknown source: {name!r}. "
                f"Registered: {list(cls._sources)}"
            )
        return source_cls(**kwargs)

    @classmethod
    def list_types(cls) -> list[str]:
        return list(cls._sources)


# 注册内置来源
SourceRegistry.register("folder", FolderSource)

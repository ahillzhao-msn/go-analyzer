"""StoreRegistry — 存储后端的工厂+注册表。"""
from .base import BaseStore
from .npz_store import NpzStore


class StoreRegistry:
    """管理所有已注册的存储后端类型。"""

    _stores: dict[str, type[BaseStore]] = {}

    @classmethod
    def register(cls, name: str, store_cls: type[BaseStore]):
        cls._stores[name] = store_cls

    @classmethod
    def create(cls, name: str, **kwargs) -> BaseStore:
        store_cls = cls._stores.get(name)
        if store_cls is None:
            raise ValueError(
                f"Unknown store: {name!r}. "
                f"Registered: {list(cls._stores)}"
            )
        return store_cls(**kwargs)

    @classmethod
    def list_types(cls) -> list[str]:
        return list(cls._stores)


# 注册内置存储后端
StoreRegistry.register("npz", NpzStore)

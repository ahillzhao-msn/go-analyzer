"""
存储抽象层 — StorageBackend 接口 + 文件实现 + SQL 协议。

架构::

    AnalysisStore  (高层, 持有一个 backend)
        │
        ├── FileStorageBackend    (默认, .npz 文件)
        ├── SqlStorageBackend     (外部 DB 的协议实现)
        └── 自定义实现: 实现 StorageBackend 接口即可

使用::

    from go_analysis.storage import AnalysisStore, FileStorageBackend

    # 文件后端 (默认)
    store = AnalysisStore(FileStorageBackend("./analysis_store"))

    # 未来: SQL 后端
    # from go_analysis.storage_sql import SqlStorageBackend
    # store = AnalysisStore(SqlStorageBackend(engine))
"""

import abc
import json
import os
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable
from dataclasses import dataclass, field

import numpy as np

from .analysis_format import AnalysisRecord


# ═══════════════════════════════════════════════════════
# 接口协议
# ═══════════════════════════════════════════════════════

@runtime_checkable
class StorageBackend(Protocol):
    """存储后端协议 — 实现此接口即可替换 AnalysisStore 的存储层。

    所有实现必须满足:
    - 线程安全 (调用方可能并发)
    - 原子 put (要么全部写入, 要么全部不写)
    - get 失败返回 None, 不抛异常
    """

    def put(self, game_id: str, record: AnalysisRecord) -> None:
        """保存一局分析。"""
        ...

    def get(self, game_id: str) -> Optional[AnalysisRecord]:
        """取回一局分析。不存在返回 None。"""
        ...

    def list_games(self) -> list[str]:
        """返回所有游戏 ID 列表。"""
        ...

    def stats(self) -> dict:
        """存储统计:
        game_count, total_mb, avg_kb
        """
        ...

    def delete(self, game_id: str) -> bool:
        """删除一局。返回 True 如果存在并删除。"""
        ...

    def close(self):
        """关闭后端, 释放资源。"""
        ...


# ═══════════════════════════════════════════════════════
# 文件后端 (默认)
# ═══════════════════════════════════════════════════════

class FileStorageBackend:
    """文件存储后端 — .npz 文件, 每个棋谱一个文件。

    这是默认后端, 与原有 AnalysisStore 行为完全兼容。
    """

    def __init__(self, store_dir: str | Path):
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def store_dir(self) -> str:
        return str(self._dir)

    def _path(self, game_id: str) -> Path:
        return self._dir / f"{game_id}.npz"

    def put(self, game_id: str, record: AnalysisRecord) -> None:
        path = self._path(game_id)
        record.to_npz(str(path))

    def get(self, game_id: str) -> Optional[AnalysisRecord]:
        path = self._path(game_id)
        if not path.exists():
            return None
        try:
            return AnalysisRecord.from_npz(str(path))
        except Exception:
            return None

    def list_games(self) -> list[str]:
        return sorted(
            f.stem for f in self._dir.iterdir()
            if f.suffix == ".npz" and f.is_file()
        )

    def stats(self) -> dict:
        games = self.list_games()
        total_bytes = sum(
            self._path(g).stat().st_size for g in games
        )
        return {
            "game_count": len(games),
            "total_mb": total_bytes / 1024 / 1024,
            "avg_kb": total_bytes / len(games) / 1024 if games else 0,
        }

    def delete(self, game_id: str) -> bool:
        path = self._path(game_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def close(self):
        pass  # 文件后端无需清理


# ═══════════════════════════════════════════════════════
# 高层 AnalysisStore
# ═══════════════════════════════════════════════════════

class AnalysisStore:
    """分析记录管理器 — 持有后端实例。

    默认使用 FileStorageBackend, 可通过 backend 参数切换。
    """

    def __init__(self, backend: StorageBackend | str | Path):
        if isinstance(backend, (str, Path)):
            # 兼容旧接口: 传目录路径 → 文件后端
            self._backend = FileStorageBackend(backend)
        elif isinstance(backend, StorageBackend):
            self._backend = backend
        else:
            raise TypeError(f"Expected StorageBackend, str, or Path, got {type(backend)}")

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    @property
    def store_dir(self) -> str:
        if isinstance(self._backend, FileStorageBackend):
            return self._backend.store_dir
        return ""

    def put(self, game_id: str, record: AnalysisRecord) -> None:
        self._backend.put(game_id, record)

    def get(self, game_id: str) -> Optional[AnalysisRecord]:
        return self._backend.get(game_id)

    def list_games(self) -> list[str]:
        return self._backend.list_games()

    def stats(self) -> dict:
        return self._backend.stats()

    def delete(self, game_id: str) -> bool:
        return self._backend.delete(game_id)

    def close(self):
        self._backend.close()


# ═══════════════════════════════════════════════════════
# 外部 DB 实现指南
# ═══════════════════════════════════════════════════════

"""
## 如何实现外部数据库后端

1. 创建新文件::

    from go_analysis.storage import StorageBackend, AnalysisRecord

    class PostgresBackend:
        \"\"\"PostgreSQL 后端. 实现 StorageBackend 协议.\"\"\"

        def __init__(self, connection_string: str):
            import psycopg2
            self.conn = psycopg2.connect(connection_string)
            self._ensure_tables()

        def _ensure_tables(self):
            with self.conn.cursor() as cur:
                cur.execute(\"\"\"
                    CREATE TABLE IF NOT EXISTS analysis_records (
                        game_id    TEXT PRIMARY KEY,
                        features   BYTEA NOT NULL,
                        global_stats BYTEA NOT NULL,
                        move_count SMALLINT NOT NULL,
                        env_hardware JSONB,
                        env_software JSONB,
                        env_game JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                \"\"\")
            self.conn.commit()

        def put(self, game_id: str, record: AnalysisRecord) -> None:
            import pickle
            with self.conn.cursor() as cur:
                cur.execute(\"\"\"
                    INSERT INTO analysis_records
                        (game_id, features, global_stats, move_count,
                         env_hardware, env_software, env_game)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
                    ON CONFLICT (game_id) DO UPDATE SET
                        features = EXCLUDED.features,
                        global_stats = EXCLUDED.global_stats,
                        move_count = EXCLUDED.move_count,
                        env_hardware = EXCLUDED.env_hardware,
                        env_software = EXCLUDED.env_software,
                        env_game = EXCLUDED.env_game;
                \"\"\", (
                    game_id,
                    pickle.dumps(record.features),
                    pickle.dumps(record.global_stats),
                    record.move_count,
                    json.dumps(record.hw.to_dict()),
                    json.dumps(record.sw.to_dict()),
                    json.dumps(record.game.to_dict()),
                ))
            self.conn.commit()

        def get(self, game_id: str) -> Optional[AnalysisRecord]:
            import pickle, json
            from .analysis_format import HardwareEnv, SoftwareEnv, GameMeta
            with self.conn.cursor() as cur:
                cur.execute(
                    \"SELECT * FROM analysis_records WHERE game_id = %s\",
                    (game_id,)
                )
                row = cur.fetchone()
            if not row:
                return None
            return AnalysisRecord(
                features=pickle.loads(row[1]),
                global_stats=pickle.loads(row[2]),
                move_count=row[3],
                hw=HardwareEnv(**json.loads(row[4])) if row[4] else HardwareEnv(),
                sw=SoftwareEnv(**json.loads(row[5])) if row[5] else SoftwareEnv(),
                game=GameMeta(**json.loads(row[6])) if row[6] else GameMeta(),
            )

        def list_games(self) -> list[str]:
            with self.conn.cursor() as cur:
                cur.execute(\"SELECT game_id FROM analysis_records ORDER BY game_id\")
                return [r[0] for r in cur.fetchall()]

        def stats(self) -> dict:
            with self.conn.cursor() as cur:
                cur.execute(\"\"\"
                    SELECT COUNT(*),
                           COALESCE(SUM(pg_column_size(features)), 0) AS feat_bytes,
                           COALESCE(SUM(pg_column_size(global_stats)), 0) AS stats_bytes
                    FROM analysis_records
                \"\"\")
                count, feat_bytes, stats_bytes = cur.fetchone()
                total_bytes = (feat_bytes or 0) + (stats_bytes or 0)
            return {
                "game_count": count,
                "total_mb": total_bytes / 1024 / 1024,
                "avg_kb": total_bytes / count / 1024 if count else 0,
            }

        def delete(self, game_id: str) -> bool:
            with self.conn.cursor() as cur:
                cur.execute(
                    \"DELETE FROM analysis_records WHERE game_id = %s\",
                    (game_id,)
                )
                deleted = cur.rowcount > 0
            self.conn.commit()
            return deleted

        def close(self):
            self.conn.close()

2. 注册到配置::

    storage:
      backend: postgres      # file | postgres
      postgres_conn: "postgresql://user:pass@host/db"
"""


# ── 向后兼容 ──────────────────────────────────────────

def make_store(store_dir_or_backend: str | Path | StorageBackend) -> AnalysisStore:
    """工厂函数, 兼容旧 API。"""
    return AnalysisStore(store_dir_or_backend)

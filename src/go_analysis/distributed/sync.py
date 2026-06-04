"""ResultSync — 分布式结果同步层。

核心原则:
  - Worker 完全离线运行 (零运行时依赖中心)
  - 同步是后置的、幂等的 (跳过已存在的)
  - 支持 SCP / robocopy / HTTP 多种传输方式
"""
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen

from ..data.store import BaseStore, NpzStore
from ..data.source import BaseSource

log = logging.getLogger("sync")


class ResultSync:
    """结果同步器。"""

    def __init__(self, central_store: BaseStore):
        self.central = central_store

    def deploy_sgfs(self, source: BaseSource, worker_path: str,
                    method: str = "scp", remote_host: str = "") -> dict:
        """将 SGF 部署到 Worker 本地目录。

        Args:
            source: 中心 SGF 来源
            worker_path: Worker 端的 training 目录路径
            method: "local" / "scp" / "robocopy"
            remote_host: SCP 时需要的 "user@host"

        Returns:
            {"total": N, "deployed": N, "skipped": N, "errors": N}
        """
        stats = {"total": source.count(), "deployed": 0, "skipped": 0, "errors": 0}

        if method == "local":
            dest = Path(worker_path)
            dest.mkdir(parents=True, exist_ok=True)
            for game_id in source.list_games():
                sgf_content, meta = source.get_game(game_id)
                group = meta.get("group", "")
                out_dir = dest / group
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"{game_id}.sgf"
                if out_path.exists():
                    stats["skipped"] += 1
                    continue
                out_path.write_text(sgf_content, encoding="utf-8")
                stats["deployed"] += 1

        elif method == "scp" and remote_host:
            # 通过 SCP 批量传输 (使用 tar 打包)
            for game_id in source.list_games():
                sgf_content, meta = source.get_game(game_id)
                group = meta.get("group", "")
                remote_dir = f"{remote_host}:{worker_path}/{group}/"
                try:
                    proc = subprocess.run(
                        ["ssh", remote_host, f"mkdir -p {worker_path}/{group}"],
                        capture_output=True, text=True, timeout=10,
                    )
                    subprocess.run(
                        ["ssh", remote_host,
                         f"cat > {worker_path}/{group}/{game_id}.sgf"],
                        input=sgf_content, text=True, timeout=30,
                    )
                    stats["deployed"] += 1
                except Exception:
                    stats["errors"] += 1
        else:
            raise ValueError(f"Unknown deploy method: {method}")

        log.info(f"Deploy: {stats['deployed']} deployed, "
                 f"{stats['skipped']} skipped, {stats['errors']} errors")
        return stats

    def pull_results(self, worker_info: dict,
                     method: str = "scp") -> int:
        """从 Worker 拉取分析结果到中心 Store。

        Args:
            worker_info: {"workdir": str, "host": str (for scp)}
            method: "local" / "scp"

        Returns:
            拉取的 NPZ 文件数量
        """
        workdir = Path(worker_info["workdir"])
        remote_store = NpzStore(str(workdir / "analysis_store"))
        synced = 0

        for game_id in remote_store.list():
            if self.central.exists(game_id):
                continue  # 幂等: 已存在的跳过
            record = remote_store.load(game_id)
            if record is not None:
                self.central.save(game_id, record)
                synced += 1

        log.info(f"Pulled {synced} results from {worker_info.get('workdir', '?')}")
        return synced

    def verify(self, coordinator_url: str) -> dict:
        """校验分析完成度。

        Returns:
            {"total": N, "synced": N, "missing": [...], "complete": bool}
        """
        resp = urlopen(f"{coordinator_url}/stats", timeout=10)
        stats = json.loads(resp.read().decode())

        total = stats["total"]
        done = stats["done"]
        missing = stats.get("remaining", 0)

        return {
            "total": total,
            "done": done,
            "missing": missing,
            "complete": missing == 0,
            "workers": stats.get("workers", {}),
        }

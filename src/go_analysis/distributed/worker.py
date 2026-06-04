"""Worker — 分布式分析工人代理。

循环: register → assign → analyze → complete
本地自持运行，零运行时依赖中心。

经验 (ROADMAP.md §2, §4):
  - per-game 独立 KataGo 进程 (避免死锁)
  - 注册时自报 done_games 去重
  - 不足 50 手标记 skip (非失败)
  - SCHTASKS 定时任务保持存活 (72h)
"""
import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

log = logging.getLogger("worker")


class Worker:
    """分布式分析工人。"""

    def __init__(self, coordinator_url: str, worker_id: str,
                 sgf_dir: str, store_dir: str,
                 katago_path: str, model_path: str,
                 config_path: Optional[str] = None,
                 visits: int = 25, min_moves: int = 10,
                 poll_interval: float = 2.0):
        self.coordinator_url = coordinator_url.rstrip("/")
        self.worker_id = worker_id
        self.sgf_dir = Path(sgf_dir)
        self.store_dir = Path(store_dir)
        self.katago_path = katago_path
        self.model_path = model_path
        self.config_path = config_path
        self.visits = visits
        self.min_moves = min_moves
        self.poll_interval = poll_interval
        self._done_games: set[str] = set()

    # ── HTTP 通信 ──

    def _post(self, endpoint: str, data: dict) -> dict:
        url = f"{self.coordinator_url}{endpoint}"
        body = json.dumps(data).encode()
        req = Request(url, data=body, headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=30)
        return json.loads(resp.read().decode())

    def _get(self, endpoint: str) -> Optional[dict]:
        url = f"{self.coordinator_url}{endpoint}"
        req = Request(url)
        try:
            resp = urlopen(req, timeout=30)
            if resp.status == 204:
                return None
            return json.loads(resp.read().decode())
        except Exception:
            return None

    # ── 分析 (委托给 Pipeline + Analyzer 抽象) ──

    def analyze_game(self, game_id: str, sgf_path: Path) -> dict:
        """分析一局棋谱 — 委托给 Pipeline 完成。

        使用虚拟化的 analyzer 适配器 + 管线抽象,
        单点维护 KataGo 进程管理逻辑 (分析器各子类)。
        """
        from ..analysis import Pipeline
        from ..analysis.sgf_parser import extract_main_line
        from ..analyzer import create_analyzer
        from ..data.source import FolderSource
        from ..data.store import NpzStore

        # 创建适配器
        analyzer_type = "windows" if ".exe" in self.katago_path else "local"
        analyzer = create_analyzer(
            analyzer_type,
            katago_path=self.katago_path,
            model_path=self.model_path,
            config_path=self.config_path,
            visits=self.visits,
        )
        source = FolderSource(str(self.sgf_dir))
        store = NpzStore(str(self.store_dir))
        pipe = Pipeline(analyzer, source, store,
                        visits=self.visits, min_moves=self.min_moves)
        result = pipe.run_one(game_id)
        return {
            "game_id": game_id,
            "status": result["status"],
            "moves": result.get("moves", 0),
            "duration_s": result.get("duration_s", 0),
            "path": result.get("path", ""),
            "error": result.get("error", ""),
        }

    # ── 主循环 ──

    def _scan_local_store(self) -> list[str]:
        """扫描本地 store 中已有的结果。"""
        from ..data.store import NpzStore
        return NpzStore(str(self.store_dir)).list()

    def run(self):
        """Worker 主循环: register → assign → analyze → complete。"""
        log.info(f"Worker {self.worker_id} starting")

        # 1. 注册 + 自报已有结果
        done_games = self._scan_local_store()
        resp = self._post("/register", {
            "worker_id": self.worker_id,
            "store_dir": str(self.store_dir),
            "done_games": done_games,
        })
        log.info(f"registered: total={resp.get('total')} "
                 f"done={resp.get('done')} remaining={resp.get('remaining')}")

        # 2. 主循环
        while True:
            task = self._get(f"/assign?worker_id={self.worker_id}")
            if task is None:
                time.sleep(self.poll_interval)
                continue

            game_id = task["game_id"]
            group = task.get("group", "")
            sgf_path = self.sgf_dir / group / f"{game_id}.sgf"
            if not sgf_path.exists():
                sgf_path = self.sgf_dir / f"{game_id}.sgf"

            if not sgf_path.exists():
                log.warning(f"SGF not found: {game_id}")
                self._post("/complete", {
                    "game_id": game_id,
                    "worker_id": self.worker_id,
                    "success": False, "move_count": 0, "duration_s": 0,
                })
                continue

            result = self.analyze_game(game_id, sgf_path)

            status_ok = result["status"] in ("ok", "skip")
            self._post("/complete", {
                "game_id": game_id,
                "worker_id": self.worker_id,
                "success": status_ok,
                "move_count": result.get("moves", 0),
                "duration_s": result.get("duration_s", 0),
                "store_path": result.get("path", ""),
            })

            if result["status"] == "ok":
                log.info(f"✓ {game_id}: {result.get('moves', 0)} moves "
                         f"({result.get('duration_s', 0):.1f}s)")
            elif result["status"] == "skip":
                log.info(f"  {game_id}: skip ({result.get('reason', '')})")
            else:
                log.warning(f"✗ {game_id}: {result.get('error', 'unknown')}")


def main():
    """命令行入口: python -m go_analysis.distributed.worker"""
    parser = argparse.ArgumentParser(description="Go Analyzer Worker")
    parser.add_argument("--coordinator", default="http://localhost:18081")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--sgf-dir", default="./training")
    parser.add_argument("--store-dir", default="./analysis_store")
    parser.add_argument("--katago", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--visits", type=int, default=25)
    parser.add_argument("--min-moves", type=int, default=10)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()

    worker_id = args.worker_id or f"worker-{os.uname().nodename}"

    worker = Worker(
        coordinator_url=args.coordinator,
        worker_id=worker_id,
        sgf_dir=args.sgf_dir,
        store_dir=args.store_dir,
        katago_path=args.katago,
        model_path=args.model,
        config_path=args.config,
        visits=args.visits,
        min_moves=args.min_moves,
        poll_interval=args.poll_interval,
    )
    worker.run()


if __name__ == "__main__":
    main()

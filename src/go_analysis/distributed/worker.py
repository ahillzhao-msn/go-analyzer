"""Worker — 完全自持的本地分析工人。

架构哲学:
  - Worker 不依赖任何外部服务 (coordinator 可选)
  - 仅监控本地 SGF 缓存, 自动分析未处理的棋谱
  - 分析结果保存到本地 store
  - 暴露 HTTP 状态接口供 coordinator 轮询

Coordinator 通过心跳轮询拉取状态和同步结果:
  GET /status  →  返回当前进度、性能统计
  POST /sync   →  coordinator 拉取未同步的结果
"""
import argparse
import json
import logging
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("worker")


class Worker:
    """完全自持的本地分析工人。"""

    def __init__(self, sgf_dir: str, store_dir: str,
                 katago_path: str, model_path: str,
                 config_path: Optional[str] = None,
                 visits: int = 25, min_moves: int = 10,
                 poll_interval: float = 5.0,
                 sync_port: int = 18083):
        self.sgf_dir = Path(sgf_dir)
        self.store_dir = Path(store_dir)
        self.katago_path = katago_path
        self.model_path = model_path
        self.config_path = config_path
        self.visits = visits
        self.min_moves = min_moves
        self.poll_interval = poll_interval
        self.sync_port = sync_port
        self._perf = self._fresh_perf()
        self._stop = False

    def _fresh_perf(self) -> dict:
        return {
            "games_analyzed": 0, "total_moves": 0, "total_duration_s": 0.0,
            "total_visits": 0, "vps_moving_avg": 0.0, "last_10": [],
        }

    # ── 核心分析逻辑 (委托给 Analyzer + Pipeline) ──

    def analyze_game(self, game_id: str, sgf_path: Path) -> dict:
        """分析一局棋谱 — 委托给 Pipeline + Analyzer 抽象完成。

        所有 KataGo 进程管理在 analyzer/ 层单点维护。
        """
        from ..analysis import Pipeline
        from ..analyzer import create_analyzer, discover_katago
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

    # ── 本地分析循环 (纯自持) ────────────────────────

    def _find_sgf(self, game_id: str) -> Optional[Path]:
        for ext in [".sgf", ".sgf.gz"]:
            for f in self.sgf_dir.rglob(f"{game_id}{ext}"):
                return f
        p = self.sgf_dir / f"{game_id}.sgf"
        return p if p.exists() else None

    def _scan_unprocessed(self) -> list[str]:
        """返回本地 SGF 中尚未分析的 game_id 列表。"""
        from ..data.source import FolderSource
        from ..data.store import NpzStore
        source = FolderSource(str(self.sgf_dir))
        store = NpzStore(str(self.store_dir))
        done = set(store.list())
        return [gid for gid in source.list_games() if gid not in done]

    def _update_perf(self, game_id: str, moves: int, dt: float, vps: float):
        self._perf["games_analyzed"] += 1
        self._perf["total_moves"] += moves
        self._perf["total_duration_s"] += dt
        self._perf["total_visits"] += moves * self.visits
        self._perf["vps_moving_avg"] = round(
            self._perf["total_visits"] / max(self._perf["total_duration_s"], 0.1), 1)
        self._perf["last_10"].append({
            "game_id": game_id, "moves": moves,
            "duration_s": round(dt, 2), "vps": vps,
        })
        if len(self._perf["last_10"]) > 10:
            self._perf["last_10"].pop(0)

    def run(self):
        """主循环: 纯本地分析, 无外部依赖。"""
        log.info(f"Worker starting (self-sufficient mode)")
        self._stop = False

        while not self._stop:
            pending = self._scan_unprocessed()
            if not pending:
                time.sleep(self.poll_interval)
                continue

            game_id = pending[0]
            sgf_path = self._find_sgf(game_id)
            if not sgf_path:
                log.warning(f"SGF not found: {game_id}")
                continue

            result = self.analyze_game(game_id, sgf_path)
            if result["status"] == "ok":
                moves = result.get("moves", 0)
                dt = result.get("duration_s", 0)
                vps = round(moves * self.visits / dt, 1) if dt > 0 else 0
                self._update_perf(game_id, moves, dt, vps)
                done = self._perf["games_analyzed"]
                log.info(f"✓ [{done}] {game_id}: {moves}m {dt:.1f}s {vps} vps")
            elif result["status"] == "skip":
                log.info(f"  {game_id}: skip ({result.get('reason', '')})")
            else:
                log.warning(f"✗ {game_id}: {result.get('error', 'unknown')}")

        log.info("Worker stopped")

    def stop(self):
        self._stop = True

    # ── 同步状态接口 (供 coordinator 轮询) ────────────

    def status(self) -> dict:
        """返回当前状态, coordinator 通过 HTTP GET 获取。"""
        from ..data.store import NpzStore
        store = NpzStore(str(self.store_dir))
        return {
            "worker_id": os.uname().nodename,
            "status": "running" if not self._stop else "stopped",
            "local_store": str(self.store_dir),
            "local_sgfs": str(self.sgf_dir),
            "games_in_store": store.count(),
            "perf": self._perf,
        }

    def serve_status(self):
        """启动 HTTP 状态服务 (供 coordinator 轮询)。"""
        worker = self

        class StatusHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/status":
                    self._respond(200, worker.status())
                else:
                    self._respond(404, {"error": "not_found"})

            def do_POST(self):
                path = urlparse(self.path).path
                if path == "/sync":
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode() if length else "{}"
                    data = json.loads(body) if body else {}
                    # coordinator 发起同步请求
                    from ..data.store import NpzStore
                    store = NpzStore(str(worker.store_dir))
                    games = store.list()
                    synced = data.get("already_have", [])
                    new_games = [g for g in games if g not in synced]
                    self._respond(200, {
                        "worker_id": os.uname().nodename,
                        "total_in_store": len(games),
                        "new_games": new_games[:100],
                        "new_count": len(new_games),
                    })
                else:
                    self._respond(404, {"error": "not_found"})

            def _respond(self, code, data):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def log_message(self, fmt, *args):
                pass

        server = HTTPServer(("0.0.0.0", self.sync_port), StatusHandler)
        log.info(f"Status server on :{self.sync_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()


def main():
    """命令行入口: python -m go_analysis.distributed.worker"""
    parser = argparse.ArgumentParser(description="Go Analyzer Worker (self-sufficient)")
    parser.add_argument("--sgf-dir", default="./training")
    parser.add_argument("--store-dir", default="./analysis_store")
    parser.add_argument("--katago", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--visits", type=int, default=25)
    parser.add_argument("--min-moves", type=int, default=10)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--sync-port", type=int, default=18083,
                        help="状态服务端口 (供 coordinator 轮询)")
    parser.add_argument("--serve-only", action="store_true",
                        help="仅启动状态服务, 不执行分析")
    args = parser.parse_args()

    worker = Worker(
        sgf_dir=args.sgf_dir, store_dir=args.store_dir,
        katago_path=args.katago, model_path=args.model,
        config_path=args.config, visits=args.visits,
        min_moves=args.min_moves, poll_interval=args.poll_interval,
        sync_port=args.sync_port,
    )

    if args.serve_only:
        worker.serve_status()
    else:
        import threading
        t = threading.Thread(target=worker.serve_status, daemon=True)
        t.start()
        try:
            worker.run()
        except KeyboardInterrupt:
            worker.stop()


if __name__ == "__main__":
    main()

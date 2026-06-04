"""Coordinator — 分布式任务调度中心。

HTTP + JSON (stdlib only), 零额外依赖。
使用经验 (ROADMAP.md §7):
  - Worker 注册时自报已有结果 (done_games)
  - 去重: merged = worker_done + store_scan
  - 超时清理: 60s 无心跳的分配自动释放
"""
import json
import logging
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock
from typing import Optional
from urllib.parse import urlparse, parse_qs

from ..data.source import BaseSource
from ..data.store import BaseStore

log = logging.getLogger("coordinator")


class Coordinator:
    """任务协调中心。"""

    def __init__(self, source: BaseSource, store: BaseStore):
        self.source = source
        self.store = store
        self._lock = Lock()
        self._workers: dict[str, dict] = {}
        self._worker_done: dict[str, set[str]] = {}
        self._assigned: dict[str, dict] = {}
        self._all_games: list[tuple[str, str]] = []  # (game_id, group)
        self._scan_all()

    def _scan_all(self):
        """扫描所有 SGF。"""
        self._all_games = []
        for gid in self.source.list_games():
            meta = self.source.get_metadata(gid)
            group = meta.get("group", "")
            self._all_games.append((gid, group))
        log.info(f"Scanned {len(self._all_games)} SGFs")

    def _known_done(self) -> set[str]:
        """返回所有已知完成/跳过的 game_id。"""
        done = set()
        # Worker 自报的已有结果
        for gids in self._worker_done.values():
            done.update(gids)
        # Store 中已存在的
        for gid in self.store.list():
            done.add(gid)
        return done

    def register(self, worker_id: str, store_dir: str = "",
                 done_games: list = None) -> dict:
        """Worker 注册。返回任务总量和已完成数。"""
        with self._lock:
            if done_games is None:
                done_games = []
            self._workers[worker_id] = {
                "last_seen": time.time(),
                "store_dir": store_dir,
            }
            if done_games:
                self._worker_done.setdefault(worker_id, set()).update(done_games)

            done = self._known_done()
            remaining = max(0, len(self._all_games) - len(done) - len(self._assigned))
            return {
                "status": "ok",
                "worker_id": worker_id,
                "total": len(self._all_games),
                "done": len(done),
                "remaining": remaining,
            }

    def assign(self, worker_id: str) -> Optional[dict]:
        """分配一个未完成的 SGF。"""
        with self._lock:
            if worker_id not in self._workers:
                return None
            self._workers[worker_id]["last_seen"] = time.time()
            self._cleanup_stale()

            done = self._known_done()
            for game_id, group in self._all_games:
                if game_id in self._assigned:
                    continue
                if game_id in done:
                    continue
                self._assigned[game_id] = {
                    "worker": worker_id, "time": time.time(),
                }
                return {"game_id": game_id, "group": group}
            return None

    def complete(self, game_id: str, worker_id: str, success: bool,
                 move_count: int = 0, duration_s: float = 0,
                 store_path: str = "") -> dict:
        """报告任务完成。"""
        with self._lock:
            self._assigned.pop(game_id, None)
            # 记录到 worker_done 以便 _known_done() 识别
            self._worker_done.setdefault(worker_id, set()).add(game_id)
        log.info(f"complete {game_id} worker={worker_id} "
                 f"{'ok' if success else 'fail'} "
                 f"moves={move_count} t={duration_s:.1f}s")
        return {"status": "ok"}

    def stats(self) -> dict:
        """全量状态。"""
        with self._lock:
            self._cleanup_stale()
            done = self._known_done()
            completed_total = len(done)
            return {
                "total": len(self._all_games),
                "done": completed_total,
                "assigned": len(self._assigned),
                "remaining": max(0, len(self._all_games) - completed_total - len(self._assigned)),
                "workers": {wid: {"store": w.get("store_dir", ""),
                                   "last_seen_s": round(time.time() - w.get("last_seen", 0), 1)}
                            for wid, w in self._workers.items()},
            }

    def _cleanup_stale(self, timeout: float = 120):
        """清理超时的分配。"""
        now = time.time()
        stale = [gid for gid, info in self._assigned.items()
                 if now - info["time"] > timeout]
        for gid in stale:
            log.warning(f"releasing stale assignment: {gid}")
            self._assigned.pop(gid, None)

    # ── HTTP Server ──

    def _make_handler(self):
        coord = self
        class CoordHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    self._respond(400, {"error": "invalid_json"})
                    return

                path = urlparse(self.path).path
                if path == "/register":
                    result = coord.register(
                        data.get("worker_id", ""),
                        data.get("store_dir", ""),
                        data.get("done_games"),
                    )
                    self._respond(200, result)
                elif path == "/complete":
                    result = coord.complete(
                        data["game_id"], data.get("worker_id", ""),
                        data.get("success", False),
                        data.get("move_count", 0),
                        data.get("duration_s", 0),
                        data.get("store_path", ""),
                    )
                    self._respond(200, result)
                else:
                    self._respond(404, {"error": "not_found"})

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/assign":
                    qs = parse_qs(urlparse(self.path).query)
                    worker_id = qs.get("worker_id", [""])[0]
                    result = coord.assign(worker_id)
                    if result:
                        self._respond(200, result)
                    else:
                        self._respond(204, {})
                elif path == "/stats":
                    self._respond(200, coord.stats())
                else:
                    self._respond(404, {"error": "not_found"})

            def _respond(self, code, data):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def log_message(self, fmt, *args):
                pass  # suppress HTTP log noise
        return CoordHandler

    def serve(self, host: str = "0.0.0.0", port: int = 18081):
        """启动 HTTP 服务 (阻塞)。"""
        server = HTTPServer((host, port), self._make_handler())
        log.info(f"Coordinator listening on {host}:{port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()


def main():
    """命令行入口: python -m go_analysis.distributed.coordinator"""
    import argparse
    parser = argparse.ArgumentParser(description="Go Analyzer Coordinator")
    parser.add_argument("--sgf-dir", default="./training", help="SGF 目录")
    parser.add_argument("--store-dir", default="./analysis_store", help="分析结果目录")
    parser.add_argument("--port", type=int, default=18081, help="端口")
    parser.add_argument("--host", default="0.0.0.0", help="绑定地址")
    args = parser.parse_args()

    from ..data.source import FolderSource
    from ..data.store import NpzStore
    source = FolderSource(args.sgf_dir)
    store = NpzStore(args.store_dir)
    coord = Coordinator(source, store)
    coord.serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()

"""Worker — 完全自持的本地分析工人 (v0.4.0)。

架构哲学:
  - Worker 不依赖任何外部服务 (coordinator 可选)
  - 仅监控本地 SGF 缓存, 自动分析未处理的棋谱
  - 分析结果保存到本地 store
  - 暴露 HTTP 状态接口供 coordinator 轮询
  - 日志: 文件 + 控制台, --log-dir, --log-level

Coordinator 集成:
  - 通过 --coordinator-url 注册状态端点 (POST /register-worker-status)
  - 后台心跳线程定期向 coordinator 注册自身
  - Coordinator 通过 GET /status 轮询拉取状态
"""
import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

log = logging.getLogger("worker")


class Worker:
    """完全自持的本地分析工人。"""

    def __init__(self, sgf_dir: str, store_dir: str,
                 katago_path: str, model_path: str,
                 config_path: Optional[str] = None,
                 visits: int = 25, min_moves: int = 10,
                 poll_interval: float = 5.0,
                 sync_port: int = 18083,
                 coordinator_url: Optional[str] = None,
                 katago_max_games: int = 50,
                 katago_max_age: int = 1800):
        self.sgf_dir = Path(sgf_dir)
        self.store_dir = Path(store_dir)
        self.katago_path = katago_path
        self.model_path = model_path
        self.config_path = config_path
        self.visits = visits
        self.min_moves = min_moves
        self.poll_interval = poll_interval
        self.sync_port = sync_port
        self.coordinator_url = coordinator_url
        self._perf = self._fresh_perf()
        self._skipped: set[str] = set()  # 已跳过的棋谱 (少于 min_moves)
        self._stop = False

        # KataGo 批量常驻管理
        self._katago_max_games = katago_max_games
        self._katago_max_age = katago_max_age
        self._analyzer = None  # 惰性创建
        self._analyzer_games = 0
        self._analyzer_start = 0.0

        # 自动检测工作模式
        self._mode = self._detect_mode()

    @staticmethod
    def _detect_mode() -> str:
        """自动检测 worker 运行模式: linux / windows / wsl_over_windows"""
        import sys as _sys
        if _sys.platform == "win32":
            return "windows"
        # WSL or native Linux: check by peeking at /proc/version
        try:
            with open("/proc/version") as _f:
                _ver = _f.read().lower()
            if "microsoft" in _ver or "wsl" in _ver:
                return "wsl_over_windows"  # WSL → Windows Katago .exe 桥接
        except Exception:
            pass
        return "linux"

    def _create_analyzer(self):
        """创建或重建 KataGo analyzer（流式常驻进程）。"""
        from ..analyzer import create_analyzer
        analyzer_type = "windows" if ".exe" in self.katago_path else "local"
        self._analyzer = create_analyzer(
            analyzer_type,
            katago_path=self.katago_path,
            model_path=self.model_path,
            config_path=self.config_path,
            visits=self.visits,
            per_move_timeout=30.0,
            max_games=self._katago_max_games,
            max_age=self._katago_max_age,
        )
        self._analyzer_games = 0
        self._analyzer_start = time.time()
        log.info(f"KataGo analyzer ready (type={analyzer_type})")

    def _need_restart(self) -> bool:
        """判断是否需要重启 KataGo 进程。"""
        if self._analyzer is None:
            return True
        if self._analyzer_games >= self._katago_max_games:
            log.info(f"KataGo restart: {self._analyzer_games} games reached limit ({self._katago_max_games})")
            return True
        age = time.time() - self._analyzer_start
        if age >= self._katago_max_age:
            log.info(f"KataGo restart: age {age:.0f}s reached limit ({self._katago_max_age}s)")
            return True
        return False

    def _fresh_perf(self) -> dict:
        return {
            "games_analyzed": 0, "total_moves": 0, "total_duration_s": 0.0,
            "total_visits": 0, "vps_moving_avg": 0.0, "last_10": [],
        }

    # ── 核心分析逻辑 (委托给 Analyzer + Pipeline, 复用 KataGo 进程) ──

    def analyze_game(self, game_id: str, sgf_path: Path) -> dict:
        """分析一局棋谱 — 复用常驻 KataGo 进程。

        每 --katago-max-games 局或 --katago-max-age 秒重启一次 KataGo，
        兼顾效率和防死锁。
        """
        from ..analysis import Pipeline
        from ..data.source import FolderSource
        from ..data.store import NpzStore

        # 检查是否需要重启 KataGo
        if self._need_restart():
            self._create_analyzer()
        assert self._analyzer is not None, "KataGo analyzer should be initialized"

        source = FolderSource(str(self.sgf_dir))
        store = NpzStore(str(self.store_dir))
        pipe = Pipeline(self._analyzer, source, store,
                        visits=self.visits, min_moves=self.min_moves)
        result = pipe.run_one(game_id)
        self._analyzer_games += 1
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

        # 启动 coordinator 注册心跳 (如果配置了 coordinator)
        if self.coordinator_url:
            def _coord_hb():
                while not self._stop:
                    try:
                        self._register_with_coordinator()
                    except Exception:
                        pass
                    time.sleep(60)
            t = threading.Thread(target=_coord_hb, daemon=True)
            t.start()
            log.info(f"Coordinator heartbeat thread started -> {self.coordinator_url}")

        while not self._stop:
            # 构建 game_id → path 映射 (避免每局独立 rglob)
            from ..data.source import FolderSource
            from ..data.store import NpzStore
            source = FolderSource(str(self.sgf_dir))
            store = NpzStore(str(self.store_dir))
            done = set(store.list())
            game_paths = {}
            for gid, path_str, _ in source._scan():
                if gid not in done and gid not in self._skipped:
                    game_paths[gid] = Path(path_str)

            if not game_paths:
                time.sleep(self.poll_interval)
                continue

            game_id, sgf_path = next(iter(game_paths.items()))

            result = self.analyze_game(game_id, sgf_path)
            if result["status"] == "ok":
                moves = result.get("moves", 0)
                dt = result.get("duration_s", 0)
                vps = round(moves * self.visits / dt, 1) if dt > 0 else 0
                self._update_perf(game_id, moves, dt, vps)
                done = self._perf["games_analyzed"]
                log.info(f"✓ [{done}] {game_id}: {moves}m {dt:.1f}s {vps} vps")
            elif result["status"] == "skip":
                self._skipped.add(game_id)
                log.info(f"  [{len(self._skipped)}] {game_id}: skip ({result.get('reason', '')})")
            else:
                self._skipped.add(game_id)
                log.warning(f"✗ [{len(self._skipped)}] {game_id}: {result.get('error', 'unknown')}")

        log.info("Worker stopped")

    def stop(self):
        self._stop = True

    def shutdown(self):
        """清理资源：关闭常驻 KataGo 进程。"""
        self._stop = True
        if self._analyzer is not None:
            try:
                self._analyzer.shutdown()
            except Exception:
                pass

    # ── 同步状态接口 (供 coordinator 轮询) ────────────

    def status(self) -> dict:
        """返回当前状态, coordinator 通过 HTTP GET 获取。"""
        from ..data.store import NpzStore
        store = NpzStore(str(self.store_dir))
        try:
            wid = os.uname().nodename
        except AttributeError:
            wid = os.environ.get("COMPUTERNAME", "unknown")
        return {
            "worker_id": wid,
            "mode": self._mode,
            "status": "running" if not self._stop else "stopped",
            "local_store": str(self.store_dir),
            "local_sgfs": str(self.sgf_dir),
            "games_in_store": store.count(),
            "perf": self._perf,
        }

    def _register_with_coordinator(self):
        """向 coordinator 注册自身状态端点。"""
        if not self.coordinator_url:
            return
        try:
            try:
                wid = os.uname().nodename
            except AttributeError:
                wid = os.environ.get("COMPUTERNAME", "unknown")
            status_url = f"http://{self._get_host_ip()}:{self.sync_port}"
            data = json.dumps({
                "worker_id": wid,
                "mode": self._mode,
                "status_url": status_url,
                "store_dir": str(self.store_dir),
            }).encode()
            req = Request(f"{self.coordinator_url}/register-worker-status",
                          data=data, method="POST",
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                if result.get("status") == "ok":
                    log.debug(f"Registered with coordinator: {wid} @ {status_url}")
                return result
        except (URLError, json.JSONDecodeError, OSError) as e:
            log.warning(f"Coordinator registration failed: {e}")
            return None

    def _get_host_ip(self) -> str:
        """获取本机 IP (用于注册状态 URL)。始终返回 127.0.0.1 避免防火墙问题。"""
        return "127.0.0.1"

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


def setup_logging(log_dir: str = "", level: str = "INFO",
                   name: str = "worker"):
    """配置 worker 日志: 文件 + 控制台。"""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger(name)

    # 控制台 handler (避免重复添加)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in logger.handlers):
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] worker: %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(console)

    # 文件 handler
    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path / "worker.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
        ))
        fh.setLevel(log_level)
        logger.addHandler(fh)

    logger.setLevel(log_level)


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
    parser.add_argument("--coordinator-url", default=None,
                        help="Coordinator 地址 (如 http://192.168.9.32:18081), 可选")
    parser.add_argument("--serve-only", action="store_true",
                        help="仅启动状态服务, 不执行分析")
    parser.add_argument("--log-dir", default="",
                        help="日志目录 (默认空=仅控制台)")
    parser.add_argument("--log-level", default="INFO",
                        help="日志级别 (DEBUG/INFO/WARNING/ERROR)")
    parser.add_argument("--katago-max-games", type=int, default=50,
                        help="每个 KataGo 进程最大分析局数 (默认 50)")
    parser.add_argument("--katago-max-age", type=int, default=1800,
                        help="每个 KataGo 进程最大存活秒数 (默认 1800=30min)")
    args = parser.parse_args()

    setup_logging(args.log_dir, args.log_level)

    try:
        default_wid = os.uname().nodename
    except AttributeError:
        default_wid = os.environ.get("COMPUTERNAME", "unknown")

    worker = Worker(
        sgf_dir=args.sgf_dir, store_dir=args.store_dir,
        katago_path=args.katago, model_path=args.model,
        config_path=args.config, visits=args.visits,
        min_moves=args.min_moves, poll_interval=args.poll_interval,
        sync_port=args.sync_port,
        coordinator_url=args.coordinator_url,
        katago_max_games=args.katago_max_games,
        katago_max_age=args.katago_max_age,
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

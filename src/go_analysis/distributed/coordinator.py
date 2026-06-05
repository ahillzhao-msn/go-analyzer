"""Coordinator — 分布式状态聚合中心。

HTTP + JSON (stdlib only), 零额外依赖。

架构转变 (v0.3.1):
  - Worker 完全自持, 不再主动注册任务
  - Coordinator 改为状态聚合 + 监控中心
  - 通过 POST /register-worker-status 注册 worker 状态端点
  - 后台轮询线程定期获取 worker 状态
  - Dashboard 显示实时聚合数据
"""
import json
import logging
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock
from typing import Optional
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import URLError

from ..data.source import BaseSource
from ..data.store import BaseStore

log = logging.getLogger("coordinator")


class Coordinator:
    """状态聚合中心 — 轮询式, 非推模式。"""

    def __init__(self, source: BaseSource, store: BaseStore,
                 poll_interval: float = 30.0):
        self.source = source
        self.store = store
        self._lock = Lock()
        self._workers: dict[str, dict] = {}           # wid → {status_url, last_seen, ...}
        self._worker_done: dict[str, set[str]] = {}    # wid → set of game_ids
        self._all_games: list[tuple[str, str]] = []    # (game_id, group)
        self._perf_history: list[dict] = []            # 采样历史
        self._last_sample_time = 0.0
        self._poll_interval = poll_interval
        self._scan_all()

        # 启动后台轮询
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        log.info(f"Background poll thread started (interval={poll_interval}s)")

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
        for gids in self._worker_done.values():
            done.update(gids)
        for gid in self.store.list():
            done.add(gid)
        return done

    # ── Worker 状态注册 (新架构: 自报端点, coordinator 轮询) ──

    def register_worker_status(self, worker_id: str, status_url: str,
                               store_dir: str = "") -> dict:
        """注册 worker 的状态端点。coordinator 会定期轮询此 URL。"""
        with self._lock:
            self._workers[worker_id] = {
                "status_url": status_url,
                "store_dir": store_dir or "",
                "last_seen": time.time(),
                "status": "registered",
                "perf": {},
                "games_in_store": 0,
            }
            log.info(f"Registered worker status endpoint: {worker_id} @ {status_url}")
            return {
                "status": "ok",
                "worker_id": worker_id,
                "total": len(self._all_games),
                "poll_interval_s": self._poll_interval,
            }

    def unregister_worker(self, worker_id: str) -> dict:
        """移除 worker 注册。"""
        with self._lock:
            self._workers.pop(worker_id, None)
            self._worker_done.pop(worker_id, None)
            log.info(f"Unregistered worker: {worker_id}")
        return {"status": "ok"}

    # ── 后台轮询 ──

    def _poll_worker(self, wid: str, info: dict):
        """轮询单个 worker 的 /status 端点。"""
        url = info.get("status_url", "")
        if not url:
            return
        try:
            req = Request(f"{url}/status", method="GET",
                          headers={"Accept": "application/json"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            with self._lock:
                if wid in self._workers:
                    self._workers[wid].update({
                        "last_seen": time.time(),
                        "status": data.get("status", data.get("state", "running")),
                        "perf": data.get("perf", {}),
                        "games_in_store": data.get("games_in_store", 0),
                        "local_store": data.get("local_store", ""),
                    })
                    # 将 worker 的 store 结果同步到 _worker_done
                    # 如果 worker 返回了已完成的 game_id 列表, 也记录下来
        except (URLError, json.JSONDecodeError, OSError) as e:
            with self._lock:
                if wid in self._workers:
                    self._workers[wid]["status"] = f"unreachable ({e})"
                    self._workers[wid]["last_seen"] = time.time()

    def _poll_loop(self):
        """后台轮询: 每 `_poll_interval` 秒轮询所有注册的 worker。"""
        while True:
            time.sleep(self._poll_interval)
            # 获取当前注册列表的快照
            with self._lock:
                snapshot = dict(self._workers)
            for wid, info in snapshot.items():
                self._poll_worker(wid, info)

    # ── 旧兼容接口 (保留但不再必需) ──

    def register(self, worker_id: str, store_dir: str = "",
                 done_games: list = None) -> dict:
        with self._lock:
            if done_games is None:
                done_games = []
            self._workers[worker_id] = {
                "last_seen": time.time(),
                "store_dir": store_dir,
                "status": "registered",
            }
            if done_games:
                self._worker_done.setdefault(worker_id, set()).update(done_games)
            done = self._known_done()
            remaining = max(0, len(self._all_games) - len(done))
            return {
                "status": "ok",
                "worker_id": worker_id,
                "total": len(self._all_games),
                "done": len(done),
                "remaining": remaining,
            }

    def assign(self, worker_id: str) -> Optional[dict]:
        return None  # 弃用: 新版 worker 自持, 不再需要 coordinator 分配

    def complete(self, game_id: str, worker_id: str, success: bool,
                 move_count: int = 0, duration_s: float = 0,
                 store_path: str = "", perf: dict = None) -> dict:
        with self._lock:
            if success:
                self._worker_done.setdefault(worker_id, set()).add(game_id)
            if perf and worker_id in self._workers:
                self._workers[worker_id]["perf"] = perf
        return {"status": "ok"}

    def stats(self) -> dict:
        """全量状态 + 性能采样。"""
        with self._lock:
            done = self._known_done()
            completed_total = len(done)
            now = time.time()

            # 每 60s 采样一次
            if now - self._last_sample_time > 60:
                total_vps = sum(
                    w.get("perf", {}).get("vps_moving_avg", 0)
                    for w in self._workers.values()
                )
                self._perf_history.append({
                    "time": now,
                    "done": completed_total,
                    "remaining": max(0, len(self._all_games) - completed_total),
                    "total_vps": round(total_vps, 1),
                    "workers": len(self._workers),
                })
                if len(self._perf_history) > 100:
                    self._perf_history.pop(0)
                self._last_sample_time = now

            return {
                "total": len(self._all_games),
                "done": completed_total,
                "assigned": 0,  # 弃用字段, 保持兼容
                "remaining": max(0, len(self._all_games) - completed_total),
                "progress_pct": round(completed_total / max(len(self._all_games), 1) * 100, 1),
                "workers": {wid: {
                    "status": w.get("status", "unknown"),
                    "status_url": w.get("status_url", ""),
                    "store": w.get("store_dir", w.get("local_store", "")),
                    "games_in_store": w.get("games_in_store", 0),
                    "last_seen_s": round(now - w.get("last_seen", 0), 1),
                    "perf": w.get("perf", {}),
                } for wid, w in self._workers.items()},
                "perf_history": self._perf_history[-20:],
            }

    def _cleanup_stale(self, timeout: float = 120):
        """清理超时 worker (标记不可达)。"""
        now = time.time()
        with self._lock:
            for wid in list(self._workers.keys()):
                if now - self._workers[wid].get("last_seen", 0) > timeout * 3:
                    if self._workers[wid].get("status", "").startswith("unreachable"):
                        continue  # 已经标记过了
                    self._workers[wid]["status"] = "stale"

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
                if path == "/register-worker-status":
                    result = coord.register_worker_status(
                        data.get("worker_id", ""),
                        data.get("status_url", ""),
                        data.get("store_dir", ""),
                    )
                    self._respond(200, result)
                elif path == "/unregister-worker":
                    result = coord.unregister_worker(data.get("worker_id", ""))
                    self._respond(200, result)
                elif path == "/register":
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
                        data.get("perf"),
                    )
                    self._respond(200, result)
                else:
                    self._respond(404, {"error": "not_found"})

            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/assign",):
                    self._respond(200, {"status": "deprecated"})
                elif path in ("/stats", "/api/stats"):
                    self._respond(200, coord.stats())
                elif path in ("/", "/dashboard"):
                    self._respond_html(200, _render_dashboard(coord.stats()))
                else:
                    self._respond(404, {"error": "not_found"})

            def _respond(self, code, data):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps(data).encode())
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def _respond_html(self, code, html: str):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html.encode())
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def log_message(self, fmt, *args):
                pass
        return CoordHandler

    def serve(self, host: str = "0.0.0.0", port: int = 18081):
        server = HTTPServer((host, port), self._make_handler())
        log.info(f"Coordinator listening on {host}:{port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()


def _render_dashboard(stats: dict) -> str:
    """HTML Dashboard。"""
    w = stats.get("workers", {})
    hist = stats.get("perf_history", [])

    # 性能趋势条形图
    bar_chart = ""
    if hist:
        max_vps = max(h.get("total_vps", 0) for h in hist) or 1
        bars = []
        for h in hist[-20:]:
            pct = h.get("total_vps", 0) / max_vps * 100
            bars.append(
                f'<div style="display:flex;align-items:center;margin:1px 0">'
                f'<span style="width:40px;font-size:10px;color:#888">{h.get("done", 0)}</span>'
                f'<div style="width:{pct:.0f}%;min-width:4px;height:12px;'
                f'background:{"#4ade80" if h.get("total_vps",0) > 200 else "#fbbf24"};'
                f'border-radius:2px"></div>'
                f'<span style="margin-left:4px;font-size:10px">{h.get("total_vps",0):.0f} vps</span>'
                f'</div>'
            )
        bar_chart = "\n".join(bars)

    total_store = sum(
        info.get("games_in_store", 0) for info in w.values()
    )
    reachable = sum(1 for info in w.values()
                    if info.get("status", "") != "unreachable" and info.get("status_url", ""))

    worker_rows = ""
    for wid, info in sorted(w.items()):
        p = info.get("perf", {})
        g = p.get("games_analyzed", 0)
        vps = p.get("vps_moving_avg", 0)
        store_count = info.get("games_in_store", 0)
        last = p.get("last_10", [])
        last_game = last[-1].get("game_id", "")[:30] if last else "-"
        last_seen = info.get("last_seen_s", 0)
        status = info.get("status", "unknown")
        status_color = "#4ade80" if status == "running" else "#f87171" if "unreachable" in status else "#fbbf24"

        worker_rows += f"""
        <tr>
            <td style="padding:4px 8px">{wid}</td>
            <td style="padding:4px 8px;color:{status_color}">{status[:20]}</td>
            <td style="padding:4px 8px">{g}</td>
            <td style="padding:4px 8px">{store_count}</td>
            <td style="padding:4px 8px">{vps:.1f}</td>
            <td style="padding:4px 8px;font-size:12px;color:#aaa">{last_seen:.0f}s</td>
            <td style="padding:4px 8px;font-size:12px;color:#aaa">{last_game}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><meta http-equiv="refresh" content="15">
<title>Go Analyzer Dashboard</title>
<style>
body {{ font-family: 'SF Mono', 'Cascadia Code', monospace; background: #1a1a2e; color: #e0e0e0; margin: 20px; }}
h1 {{ color: #4ade80; font-size: 20px; }}
h2 {{ color: #fbbf24; font-size: 14px; margin-top: 20px; }}
.card {{ background: #16213e; border-radius: 8px; padding: 16px; margin: 8px 0; }}
.stat {{ display: inline-block; margin: 0 20px 8px 0; }}
.stat-val {{ font-size: 28px; font-weight: bold; color: #4ade80; }}
.stat-label {{ font-size: 11px; color: #888; }}
table {{ border-collapse: collapse; width: 100%; }}
th {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #333; color: #888; font-size: 11px; }}
td {{ padding: 4px 8px; border-bottom: 1px solid #222; font-size: 13px; }}
.bar-container {{ margin: 8px 0; }}
</style>
</head><body>
<h1>⚫ Go Analyzer · 分析集群</h1>
<div class="card">
    <div class="stat"><div class="stat-val">{stats.get("done", 0)}</div><div class="stat-label">完成</div></div>
    <div class="stat"><div class="stat-val">{stats.get("remaining", 0)}</div><div class="stat-label">剩余</div></div>
    <div class="stat"><div class="stat-val">{stats.get("progress_pct", 0)}%</div><div class="stat-label">进度</div></div>
    <div class="stat"><div class="stat-val">{len(w)}</div><div class="stat-label">已注册</div></div>
    <div class="stat"><div class="stat-val">{reachable}</div><div class="stat-label">在线</div></div>
    <div class="stat"><div class="stat-val">{total_store}</div><div class="stat-label">库内总数</div></div>
</div>

<h2>📊 Workers</h2>
<div class="card">
<table>
<tr><th>ID</th><th>状态</th><th>本局分析</th><th>库内棋谱</th><th>VPS</th><th>上次心跳</th><th>最后棋局</th></tr>
{worker_rows}
</table>
</div>

<h2>📈 VPS 趋势 (最近 20 采样)</h2>
<div class="card bar-container">{bar_chart or "<span style='color:#666'>等待数据...</span>"}</div>

<h2>📋 接口</h2>
<div class="card" style="font-size:12px;color:#aaa">
    <div>/dashboard — 本页</div>
    <div>/stats    — JSON 状态</div>
    <div><code>POST /register-worker-status</code> — 注册 worker 状态端点</div>
</div>
<p style="font-size:10px;color:#555;margin-top:20px">
    自动刷新: 15s / 更新时间: {time.strftime("%H:%M:%S")}
</p>
</body></html>"""


def main():
    """命令行入口: python -m go_analysis.distributed.coordinator"""
    import argparse
    parser = argparse.ArgumentParser(description="Go Analyzer Coordinator")
    parser.add_argument("--sgf-dir", default="./training", help="SGF 目录")
    parser.add_argument("--store-dir", default="./analysis_store", help="分析结果目录")
    parser.add_argument("--port", type=int, default=18081, help="端口")
    parser.add_argument("--host", default="0.0.0.0", help="绑定地址")
    parser.add_argument("--poll-interval", type=float, default=30.0,
                        help="Worker 状态轮询间隔 (秒)")
    args = parser.parse_args()

    from ..data.source import FolderSource
    from ..data.store import NpzStore
    source = FolderSource(args.sgf_dir)
    store = NpzStore(args.store_dir)
    coord = Coordinator(source, store, poll_interval=args.poll_interval)
    coord.serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()

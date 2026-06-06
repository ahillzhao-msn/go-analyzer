"""Coordinator — 分布式状态聚合中心 (v0.4.0: SQLite 持久化 + 双节点 failover)。

HTTP + JSON (stdlib only), 零额外依赖。

架构:
  - 每台主机运行本地 coordinator + 本地 worker
  - Worker 向本地 coordinator 注册 (127.0.0.1)
  - Coordinator 后台同步状态到 peer 节点 (--peer)
  - SQLite 持久化: 重启后恢复所有 worker 注册和性能历史
  - 任意节点 dashboard 展示全局统一视图
"""
import json
import logging
import os
import sqlite3
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

from ..data.source import BaseSource
from ..data.store import BaseStore

log = logging.getLogger("coordinator")

# ── SQLite schema ──────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    mode TEXT DEFAULT 'unknown',
    status_url TEXT,
    store_dir TEXT DEFAULT '',
    last_seen REAL DEFAULT 0,
    status TEXT DEFAULT 'registered',
    games_in_store INTEGER DEFAULT 0,
    perf TEXT DEFAULT '{}',
    local_store TEXT DEFAULT '',
    source TEXT DEFAULT 'local'
);
CREATE TABLE IF NOT EXISTS perf_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    done INTEGER DEFAULT 0,
    remaining INTEGER DEFAULT 0,
    total_vps REAL DEFAULT 0,
    workers INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS peers (
    peer_url TEXT PRIMARY KEY,
    node_id TEXT DEFAULT '',
    last_sync REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_perf_ts ON perf_history(ts);
"""


def _detect_node_id() -> str:
    import socket
    try:
        return socket.gethostname()
    except Exception:
        try:
            return os.uname().nodename
        except Exception:
            return "unknown"


class Coordinator:
    """状态聚合中心 — 持久化 + 双节点 failover。"""

    def __init__(self, source: BaseSource, store: BaseStore,
                 poll_interval: float = 30.0,
                 node_id: str = "",
                 peer_url: Optional[str] = None,
                 data_dir: str = ""):
        self.source = source
        self.store_obj = store
        self.node_id = node_id or _detect_node_id()
        self.peer_url = peer_url
        self._lock = Lock()
        self._all_games: list[tuple[str, str]] = []
        self._poll_interval = poll_interval
        self._last_sample_time = 0.0

        # 数据目录 (SQLite 文件)
        self._data_dir = Path(data_dir) if data_dir else Path.cwd() / "coordinator_data"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._data_dir / "coordinator.db"
        self._init_db()

        # 恢复 worker 注册到内存
        self._workers: dict[str, dict] = {}
        self._worker_done: dict[str, set[str]] = {}
        self._load_workers_from_db()

        # 恢复 perf_history
        self._perf_history: list[dict] = []
        self._load_perf_history()

        self._scan_all()

        # 后台线程
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        log.info(f"Background poll thread started (interval={poll_interval}s)")

        if self.peer_url:
            self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
            self._sync_thread.start()
            log.info(f"Peer sync thread started -> {self.peer_url}")

    # ── SQLite ─────────────────────────────────────────

    def _init_db(self):
        conn = sqlite3.connect(str(self._db_path))
        conn.executescript(SCHEMA)
        # 迁移: 添加 mode 列 (如果不存在)
        try:
            conn.execute("ALTER TABLE workers ADD COLUMN mode TEXT DEFAULT 'unknown'")
        except sqlite3.OperationalError:
            pass  # 列已存在
        conn.commit()
        conn.close()
        log.info(f"Coordinator DB: {self._db_path}")

    def _save_worker(self, wid: str, info: dict):
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("""
            INSERT OR REPLACE INTO workers
            (worker_id, mode, status_url, store_dir, last_seen, status,
             games_in_store, perf, local_store, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            wid,
            info.get("mode", "unknown"),
            info.get("status_url", ""),
            info.get("store_dir", info.get("local_store", "")),
            info.get("last_seen", time.time()),
            info.get("status", "registered"),
            info.get("games_in_store", 0),
            json.dumps(info.get("perf", {})),
            info.get("local_store", ""),
            info.get("source", self.node_id),
        ))
        conn.commit()
        conn.close()

    def _remove_worker(self, wid: str):
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("DELETE FROM workers WHERE worker_id = ?", (wid,))
        conn.commit()
        conn.close()

    def _load_workers_from_db(self):
        conn = sqlite3.connect(str(self._db_path))
        rows = conn.execute("SELECT * FROM workers").fetchall()
        conn.close()
        cols = ["worker_id", "mode", "status_url", "store_dir", "last_seen", "status",
                "games_in_store", "perf", "local_store", "source"]
        for row in rows:
            info = dict(zip(cols, row))
            wid = info.pop("worker_id")
            try:
                info["perf"] = json.loads(info.get("perf", "{}"))
            except (json.JSONDecodeError, TypeError):
                info["perf"] = {}
            self._workers[wid] = info
        if rows:
            log.info(f"Restored {len(rows)} workers from DB")

    def _save_perf_sample(self, sample: dict):
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("""
            INSERT INTO perf_history (ts, done, remaining, total_vps, workers)
            VALUES (?, ?, ?, ?, ?)
        """, (
            sample.get("time", time.time()),
            sample.get("done", 0),
            sample.get("remaining", 0),
            sample.get("total_vps", 0),
            sample.get("workers", 0),
        ))
        conn.commit()
        conn.close()

    def _load_perf_history(self, limit: int = 200):
        conn = sqlite3.connect(str(self._db_path))
        rows = conn.execute("""
            SELECT ts, done, remaining, total_vps, workers
            FROM perf_history ORDER BY ts DESC LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        self._perf_history = [
            {"time": r[0], "done": r[1], "remaining": r[2],
             "total_vps": r[3], "workers": r[4]}
            for r in reversed(rows)
        ]

    def _save_peer(self, peer_url: str, node_id: str):
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("""
            INSERT OR REPLACE INTO peers (peer_url, node_id, last_sync)
            VALUES (?, ?, ?)
        """, (peer_url, node_id, time.time()))
        conn.commit()
        conn.close()

    # ── 扫描 ───────────────────────────────────────────

    def _scan_all(self):
        self._all_games = []
        for gid in self.source.list_games():
            meta = self.source.get_metadata(gid)
            group = meta.get("group", "")
            self._all_games.append((gid, group))
        log.info(f"Scanned {len(self._all_games)} SGFs")

    def _known_done(self) -> set[str]:
        done = set()
        for gids in self._worker_done.values():
            done.update(gids)
        for gid in self.store_obj.list():
            done.add(gid)
        return done

    # ── Worker 注册 ────────────────────────────────────
    def register_worker_status(self, worker_id: str, status_url: str,
                               store_dir: str = "", mode: str = "unknown") -> dict:
        """注册 worker 的状态端点。coordinator 定期轮询此 URL。"""
        with self._lock:
            info = {
                "status_url": status_url,
                "store_dir": store_dir or "",
                "mode": mode,
                "last_seen": time.time(),
                "status": "registered",
                "perf": {},
                "games_in_store": 0,
                "local_store": store_dir or "",
                "source": self.node_id,
            }
            self._workers[worker_id] = info
            self._save_worker(worker_id, info)
            log.info(f"Registered worker: {worker_id} @ {status_url}")
            return {
                "status": "ok", "worker_id": worker_id,
                "total": len(self._all_games),
                "poll_interval_s": self._poll_interval,
            }

    def unregister_worker(self, worker_id: str) -> dict:
        with self._lock:
            self._workers.pop(worker_id, None)
            self._worker_done.pop(worker_id, None)
            self._remove_worker(worker_id)
        return {"status": "ok"}

    # ── 后台轮询 ───────────────────────────────────────

    def _poll_worker(self, wid: str, info: dict):
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
                    self._save_worker(wid, self._workers[wid])
        except (URLError, json.JSONDecodeError, OSError) as e:
            with self._lock:
                if wid in self._workers:
                    self._workers[wid]["status"] = f"unreachable ({e})"
                    self._workers[wid]["last_seen"] = time.time()
                    self._save_worker(wid, self._workers[wid])

    def _poll_loop(self):
        while True:
            time.sleep(self._poll_interval)
            with self._lock:
                snapshot = dict(self._workers)
            for wid, info in snapshot.items():
                self._poll_worker(wid, info)

    # ── Peer 同步 ──────────────────────────────────────

    def _collect_snapshot(self) -> dict:
        with self._lock:
            workers_safe = {}
            for wid, info in self._workers.items():
                workers_safe[wid] = dict(info)
            return {
                "node_id": self.node_id,
                "timestamp": time.time(),
                "workers": workers_safe,
                "perf_history": self._perf_history[-30:],
                "total_games": len(self._all_games),
            }

    def _sync_to_peer(self):
        if not self.peer_url:
            return
        try:
            snapshot = self._collect_snapshot()
            data = json.dumps(snapshot).encode()
            req = Request(f"{self.peer_url}/sync", data=data, method="POST",
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
                if result.get("status") == "ok":
                    self._save_peer(self.peer_url, result.get("node_id", "?"))
                    log.debug(f"Synced to peer {self.peer_url}: "
                              f"{len(snapshot['workers'])} workers")
                return result
        except (URLError, json.JSONDecodeError, OSError) as e:
            log.warning(f"Peer sync failed ({self.peer_url}): {e}")
            return None

    def _sync_loop(self):
        while True:
            time.sleep(60)
            self._sync_to_peer()

    def merge_peer_snapshot(self, snapshot: dict) -> dict:
        peer_node = snapshot.get("node_id", "unknown")
        peer_workers = snapshot.get("workers", {})
        peer_history = snapshot.get("perf_history", [])
        merged_count = 0

        with self._lock:
            for wid, info in peer_workers.items():
                if wid not in self._workers:
                    info["source"] = f"peer:{peer_node}"
                    info["last_seen"] = info.get("last_seen", time.time())
                    self._workers[wid] = dict(info)
                    self._save_worker(wid, self._workers[wid])
                    merged_count += 1
                else:
                    existing = self._workers[wid]
                    if existing.get("source", "").startswith("peer:"):
                        if info.get("last_seen", 0) > existing.get("last_seen", 0):
                            self._workers[wid].update(info)
                            self._workers[wid]["source"] = f"peer:{peer_node}"
                            self._save_worker(wid, self._workers[wid])

            existing_times = {h.get("time", 0) for h in self._perf_history}
            for h in peer_history:
                t = h.get("time", 0)
                if t and t not in existing_times:
                    self._perf_history.append(h)
                    existing_times.add(t)
                    self._save_perf_sample(h)
            self._perf_history.sort(key=lambda x: x.get("time", 0))
            if len(self._perf_history) > 200:
                self._perf_history = self._perf_history[-200:]

        log.info(f"Merged {merged_count} workers from peer {peer_node}")
        return {"status": "ok", "merged": merged_count, "node_id": self.node_id}

    # ── 旧兼容接口 ─────────────────────────────────────

    def register(self, worker_id: str, store_dir: str = "",
                 done_games: list = None) -> dict:
        with self._lock:
            if done_games is None:
                done_games = []
            info = {
                "last_seen": time.time(),
                "store_dir": store_dir,
                "status": "registered",
                "source": self.node_id,
                "perf": {},
            }
            self._workers[worker_id] = info
            self._save_worker(worker_id, info)
            if done_games:
                self._worker_done.setdefault(worker_id, set()).update(done_games)
            done = self._known_done()
            remaining = max(0, len(self._all_games) - len(done))
            return {
                "status": "ok", "worker_id": worker_id,
                "total": len(self._all_games), "done": len(done),
                "remaining": remaining,
            }

    def assign(self, worker_id: str) -> Optional[dict]:
        return None

    def complete(self, game_id: str, worker_id: str, success: bool,
                 move_count: int = 0, duration_s: float = 0,
                 store_path: str = "", perf: dict = None) -> dict:
        with self._lock:
            if success:
                self._worker_done.setdefault(worker_id, set()).add(game_id)
            if perf and worker_id in self._workers:
                self._workers[worker_id]["perf"] = perf
                self._save_worker(worker_id, self._workers[worker_id])
        return {"status": "ok"}

    def stats(self) -> dict:
        with self._lock:
            done = self._known_done()
            completed_total = len(done)
            now = time.time()

            if now - self._last_sample_time > 60:
                total_vps = sum(
                    w.get("perf", {}).get("vps_moving_avg", 0)
                    for w in self._workers.values()
                )
                sample = {
                    "time": now, "done": completed_total,
                    "remaining": max(0, len(self._all_games) - completed_total),
                    "total_vps": round(total_vps, 1),
                    "workers": len(self._workers),
                }
                self._perf_history.append(sample)
                self._save_perf_sample(sample)
                if len(self._perf_history) > 100:
                    self._perf_history.pop(0)
                self._last_sample_time = now

            return {
                "total": len(self._all_games),
                "done": completed_total,
                "remaining": max(0, len(self._all_games) - completed_total),
                "progress_pct": round(completed_total / max(len(self._all_games), 1) * 100, 1),
                "workers": {wid: {
                    "mode": w.get("mode", "unknown"),
                    "status": w.get("status", "unknown"),
                    "status_url": w.get("status_url", ""),
                    "store": w.get("store_dir", w.get("local_store", "")),
                    "games_in_store": w.get("games_in_store", 0),
                    "last_seen_s": round(now - w.get("last_seen", 0), 1),
                    "perf": w.get("perf", {}),
                } for wid, w in self._workers.items()},
                "perf_history": self._perf_history[-20:],
                "node_id": self.node_id,
            }

    # ── HTTP Server ────────────────────────────────────

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
                        data.get("mode", "unknown"),
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
                elif path == "/sync":
                    result = coord.merge_peer_snapshot(data)
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
    """HTML Dashboard — 展示全局 Worker 状态 + 趋势。"""
    import html
    w = stats.get("workers", {})
    hist = stats.get("perf_history", [])
    total = stats.get("total", 8003)
    coord_done = stats.get("done", 0)
    now = time.time()

    total_store = sum(info.get("games_in_store", 0) for info in w.values())
    combined_vps = sum(info.get("perf", {}).get("vps_moving_avg", 0) for info in w.values())
    reachable = sum(1 for info in w.values()
                    if info.get("status", "") not in ("unreachable", "") and info.get("status_url", ""))

    # ── 全局预估 ──
    total_worker_npz = total_store
    remaining = max(0, total - total_worker_npz)
    if combined_vps > 0 and remaining > 0:
        eta_s = remaining * 70 / max(combined_vps / 400, 0.1)
        eta_str = f"{eta_s/3600:.1f}h" if eta_s > 3600 else f"{eta_s/60:.0f}m"
    else:
        eta_str = "—"
    pct_all = (total_worker_npz / total * 100) if total else 0
    pct_bar_w = min(pct_all, 100)

    # ── VPS 趋势 ──
    valid_hist = [h for h in hist if h.get("total_vps", 0) > 5]
    bar_chart = ""
    if valid_hist:
        max_vps = max(h.get("total_vps", 0) for h in valid_hist) or 1
        bars = []
        for h in valid_hist[-30:]:
            pct = h.get("total_vps", 0) / max_vps * 100
            v = h.get("total_vps", 0)
            d = h.get("done", 0)
            color = "#4ade80" if v > 300 else "#fbbf24" if v > 100 else "#f87171"
            bars.append(
                f'<div style="display:flex;align-items:center;margin:2px 0;gap:4px">'
                f'<span style="width:36px;font-size:10px;color:#888;text-align:right">{d}</span>'
                f'<div style="flex:1;height:14px;background:#0f172a;border-radius:3px;overflow:hidden">'
                f'<div style="width:{pct:.0f}%;height:100%;background:{color};border-radius:3px;'
                f'transition:width 0.5s"></div></div>'
                f'<span style="width:50px;font-size:10px;color:#aaa">{v:.0f} vps</span>'
                f'</div>'
            )
        bar_chart = "\n".join(bars)

    # ── Worker 行 ──
    worker_rows = ""
    for wid, info in sorted(w.items()):
        p = info.get("perf", {})
        games = p.get("games_analyzed", 0)
        store_npz = info.get("games_in_store", 0)
        vps = p.get("vps_moving_avg", 0)
        total_moves = p.get("total_moves", 0)
        total_dur = p.get("total_duration_s", 0)
        last10 = p.get("last_10", [])
        last_game = last10[-1].get("game_id", "")[:32] if last10 else "—"
        last_seen = info.get("last_seen_s", 999)
        status = info.get("status", "unknown")
        mode = info.get("mode", "?")
        src = info.get("source", "?")

        # 模式图标
        mode_icon = {"linux": "🐧", "windows": "🪟", "wsl_over_windows": "🔄"}.get(mode, "❓")

        if status == "running":
            icon, s_color = "●", "#4ade80"
        elif "unreachable" in status:
            icon, s_color = "○", "#f87171"
        elif status == "registered":
            icon, s_color = "◐", "#fbbf24"
        else:
            icon, s_color = "○", "#555"

        w_remain = max(0, total - max(games, store_npz))
        if vps > 0 and w_remain > 0:
            avg_s = total_dur / max(games, 1) if games > 0 else 70
            eta_s = w_remain * avg_s
            eta_str_w = f"{eta_s/3600:.1f}h" if eta_s > 3600 else f"{eta_s/60:.0f}m"
        else:
            eta_str_w = "—"

        src_tag = f"<span style='font-size:10px;color:#555'> [{src[:12]}]</span>" if src and src != "?" else ""

        worker_rows += f"""
        <tr>
            <td style="padding:6px 8px;color:{s_color}">{icon} {html.escape(wid)}{src_tag}</td>
            <td style="padding:6px 8px;font-size:11px;text-align:center" title="{mode}">{mode_icon}</td>
            <td style="padding:6px 8px;color:{s_color};font-size:12px">{status[:15]}</td>
            <td style="padding:6px 8px;font-weight:bold">{store_npz}</td>
            <td style="padding:6px 8px">{games}</td>
            <td style="padding:6px 8px">{total_moves}</td>
            <td style="padding:6px 8px">{vps:.0f}</td>
            <td style="padding:6px 8px">{eta_str_w}</td>
            <td style="padding:6px 8px;font-size:11px;color:#aaa">{last_seen:.0f}s</td>
            <td style="padding:6px 8px;font-size:11px;color:#aaa;max-width:180px;overflow:hidden;text-overflow:ellipsis">{last_game}</td>
        </tr>"""

    time_str = time.strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><meta http-equiv="refresh" content="15">
<title>Go Analyzer Dashboard</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; background: #0f172a; color: #e2e8f0; margin: 0; padding: 20px; }}
h1 {{ color: #4ade80; font-size: 18px; margin-bottom: 16px; display:flex; align-items:center; gap:8px; }}
h2 {{ color: #fbbf24; font-size: 13px; margin-top: 20px; margin-bottom: 8px; text-transform:uppercase; letter-spacing:0.5px; }}
.card {{ background: #1e293b; border-radius: 10px; padding: 16px; margin: 8px 0; border: 1px solid #334155; }}
.stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 12px; }}
.stat {{ text-align: center; }}
.stat-val {{ font-size: 24px; font-weight: bold; }}
.stat-label {{ font-size: 10px; color: #64748b; margin-top: 2px; }}
.progress-bar {{ height: 8px; background: #0f172a; border-radius: 4px; overflow: hidden; }}
.progress-fill {{ height: 100%; background: linear-gradient(90deg, #4ade80, #22d3ee); border-radius: 4px; transition: width 1s; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
th {{ text-align: left; padding: 8px 8px; border-bottom: 1px solid #334155; color: #64748b; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; }}
td {{ padding: 6px 8px; border-bottom: 1px solid #1e293b; white-space: nowrap; }}
tr:hover td {{ background: #1e3a5f40; }}
.bar-container {{ padding: 8px 0; }}
.footer {{ font-size: 10px; color: #475569; text-align: center; margin-top: 24px; border-top: 1px solid #1e293b; padding-top: 12px; }}
a {{ color: #22d3ee; text-decoration: none; }}
code {{ background: #0f172a; padding: 1px 5px; border-radius: 3px; font-size: 11px; }}
</style>
</head><body>
<h1>⚫ Go Analyzer · 分析集群 <span style="font-size:12px;color:#64748b;font-weight:normal">{stats.get("node_id","?")}</span></h1>

<div class="card">
<div class="stat-grid">
    <div class="stat"><div class="stat-val" style="color:#4ade80">{coord_done}</div><div class="stat-label">Coordinator 库内</div></div>
    <div class="stat"><div class="stat-val" style="color:#22d3ee">{total_store}</div><div class="stat-label">Worker 合计 NPZ</div></div>
    <div class="stat"><div class="stat-val" style="color:#fbbf24">{total}</div><div class="stat-label">棋谱总数</div></div>
    <div class="stat"><div class="stat-val" style="color:#f87171">{remaining}</div><div class="stat-label">剩余</div></div>
    <div class="stat"><div class="stat-val" style="color:#a78bfa">{len(w)}</div><div class="stat-label">Worker</div></div>
    <div class="stat"><div class="stat-val" style="color:#4ade80">{combined_vps:.0f}</div><div class="stat-label">合并 VPS</div></div>
    <div class="stat"><div class="stat-val" style="color:#fbbf24;font-size:16px">{eta_str}</div><div class="stat-label">预估剩余</div></div>
</div>
<div class="progress-bar" style="margin-top:12px">
    <div class="progress-fill" style="width:{pct_bar_w:.1f}%"></div>
</div>
<div style="display:flex;justify-content:space-between;font-size:11px;color:#64748b;margin-top:4px">
    <span>0</span>
    <span>{total_worker_npz} / {total} ({pct_all:.1f}%)</span>
    <span>{total}</span>
</div>
</div>

<h2>📊 Worker 详情</h2>
<div class="card" style="overflow-x:auto">
<table>\n<tr><th>Worker</th><th>模式</th><th>状态</th><th>NPZ</th><th>分析局数</th><th>总手数</th><th>VPS</th><th>预估</th><th>心跳</th><th>最后棋局</th></tr>
{worker_rows}
</table>
</div>

<h2>📈 VPS 趋势</h2>
<div class="card bar-container">{bar_chart or '<span style="color:#64748b;font-size:12px">等待数据...</span>'}</div>

<div class="card" style="font-size:11px;color:#64748b">
    <div><strong style="color:#94a3b8">接口:</strong> <code>/dashboard</code> · <code>/stats</code> · <code>POST /register-worker-status</code> · <code>POST /sync</code></div>
    <div style="margin-top:4px">🔄 15s 自动刷新 · {time_str}</div>
</div>
<div class="footer">go-analyzer v0.4.0 · SQLite 持久化 · 双节点 failover · <a href="/stats">/stats</a></div>
</body></html>"""


def setup_logging(log_dir: str = "", level: str = "INFO"):
    """配置协调器日志: 文件 + 控制台。"""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # 控制台 handler
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] coordinator: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logging.getLogger("coordinator").addHandler(console)

    # 文件 handler
    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path / "coordinator.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
        ))
        fh.setLevel(log_level)
        logging.getLogger("coordinator").addHandler(fh)

    logging.getLogger("coordinator").setLevel(log_level)


def main():
    """命令行入口: python -m go_analysis.distributed.coordinator"""
    import argparse
    parser = argparse.ArgumentParser(description="Go Analyzer Coordinator (v0.4.0 failover)")
    parser.add_argument("--sgf-dir", default="./training", help="SGF 目录")
    parser.add_argument("--store-dir", default="./analysis_store", help="分析结果目录")
    parser.add_argument("--port", type=int, default=18081, help="端口")
    parser.add_argument("--host", default="0.0.0.0", help="绑定地址")
    parser.add_argument("--poll-interval", type=float, default=30.0, help="Worker 轮询间隔 (秒)")
    parser.add_argument("--node-id", default="", help="本机节点名 (默认自动检测)")
    parser.add_argument("--peer", default=None, help="Peer coordinator URL")
    parser.add_argument("--data-dir", default="", help="数据目录 (SQLite + 日志)")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    args = parser.parse_args()

    setup_logging(args.data_dir or "", args.log_level)

    from ..data.source import FolderSource
    from ..data.store import NpzStore
    source = FolderSource(args.sgf_dir)
    store = NpzStore(args.store_dir)
    coord = Coordinator(source, store, poll_interval=args.poll_interval,
                        node_id=args.node_id, peer_url=args.peer,
                        data_dir=args.data_dir)
    coord.serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()

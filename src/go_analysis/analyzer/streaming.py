"""StreamingWindowsAnalyzer — 流式 KataGo 分析器。

核心设计:
  - 一个常驻 KataGo 进程跨多棋谱复用
  - 逐手发送查询（非整局批量），逐手读取响应
  - 每手超时（默认 30s），超时后杀进程重启，从该手重试
  - 每 N 局或 T 秒刷新进程（防内存泄漏）
  - 健康探针：每局结束后发一条轻量 ping

比批量模式优势:
  1. 无进程启动开销（~5s/局）
  2. 单手超时而非整批超时，粒度更细
  3. 死锁只丢一手，不是 50 手
  4. 进程可跨局复用，GPU 预热持续
"""
import json
import subprocess
import threading
import time
from typing import Optional

from .base import BaseAnalyzer, AnalysisResult, extract_12dim_features


HEARTBEAT_MOVES = [["B", "pd"]]


class StreamingWindowsAnalyzer(BaseAnalyzer):
    """流式常驻 KataGo 分析器。"""

    def __init__(self, katago_path: str, model_path: str,
                 config_path: Optional[str] = None,
                 visits: int = 25,
                 per_move_timeout: float = 30.0,
                 max_games: int = 50,
                 max_age: float = 1800.0):
        self.katago_path = katago_path
        self.model_path = model_path
        self.config_path = config_path
        self.visits = visits
        self.per_move_timeout = per_move_timeout
        self.max_games = max_games
        self.max_age = max_age

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._games_served = 0
        self._born_at = time.time()

    # ── 进程管理 ────────────────────────────────────────

    def _start_proc(self):
        """启动 KataGo 进程。"""
        cmd = [self.katago_path, "analysis", "-model", self.model_path]
        if self.config_path:
            cmd += ["-config", self.config_path]
        CREATE_NO_WINDOW = 0x08000000
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )

    def _kill_proc(self):
        """强制终止 KataGo 进程。"""
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            proc.terminate()
            proc.wait(3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _ensure_alive(self) -> bool:
        """确保进程存活且健康。如果需要则重启。"""
        now = time.time()
        needs_restart = False

        if self._proc is None:
            needs_restart = True
        elif self._proc.poll() is not None:
            needs_restart = True
        elif self._games_served >= self.max_games:
            needs_restart = True
        elif now - self._born_at >= self.max_age:
            needs_restart = True

        if needs_restart:
            self._kill_proc()
            try:
                self._start_proc()
                self._games_served = 0
                self._born_at = time.time()
            except Exception:
                self._proc = None
                return False
        return True

    def health_check(self) -> bool:
        """健康探针：发一条轻量查询确认进程存活。"""
        with self._lock:
            if not self._ensure_alive():
                return False
            return self._query_one(HEARTBEAT_MOVES, timeout=10.0) is not None

    # ── 单查询 ──────────────────────────────────────────

    def _query_one(self, moves: list, timeout: float) -> Optional[dict]:
        """发送一条查询，等待响应。超时返回 None。"""
        proc = self._proc
        if proc is None:
            return None

        query = json.dumps({
            "id": "q",
            "moves": moves,
            "maxVisits": self.visits,
            "rules": "chinese", "komi": 7.5,
            "boardXSize": 19, "boardYSize": 19,
            "includePolicy": True,
        })

        try:
            proc.stdin.write(query + "\n")
            proc.stdin.flush()
        except Exception:
            return None

        # 带超时的逐行读取
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = proc.stdout.readline()
                if not line:
                    return None
                resp = json.loads(line.strip())
                if resp.get("id") == "q":
                    return resp
            except Exception:
                continue
        return None  # 超时

    # ── 分析接口 ────────────────────────────────────────

    def analyze(self, moves: list) -> AnalysisResult:
        """分析一局棋谱。逐手发送，逐手读取，单手超时。"""
        if not moves:
            return AnalysisResult(success=True, features=[])

        n = len(moves)
        t0 = time.time()
        raw_responses: dict[int, dict] = {}
        move_offset = 0
        overall_success = True

        while move_offset < n:
            with self._lock:
                alive = self._ensure_alive()
                if not alive:
                    overall_success = False
                    break

                # 从头到当前手的完整棋盘状态
                cur_moves = moves[:move_offset + 1]
                resp = self._query_one(cur_moves, self.per_move_timeout)

            if resp is None:
                # 超时或进程死——杀进程，下一轮重启重试
                with self._lock:
                    self._kill_proc()
                continue  # 重试同一手（不递增 move_offset）

            # 解析响应
            move_infos = resp.get("moveInfos", [])
            root_info = resp.get("rootInfo", {})
            raw_responses[move_offset] = {
                "id": f"g_{move_offset}",
                "moveInfos": move_infos,
                "rootInfo": root_info,
            }
            move_offset += 1

        with self._lock:
            self._games_served += 1

        dt = time.time() - t0

        if not raw_responses:
            return AnalysisResult(success=False, duration_s=dt)

        features = extract_12dim_features(raw_responses, moves[:len(raw_responses)])
        return AnalysisResult(features=features, duration_s=dt,
                              success=overall_success, visits_used=self.visits)

    # ── 清理 ────────────────────────────────────────────

    def shutdown(self):
        """关闭常驻进程。"""
        with self._lock:
            self._kill_proc()

    def __del__(self):
        self.shutdown()

    # ── 基准 ────────────────────────────────────────────

    def benchmark(self, test_moves: list, visits_range: list = None) -> dict:
        if visits_range is None:
            visits_range = [25, 50, 100, 200]
        original = self.visits
        results = []
        for v in visits_range:
            self.visits = v
            t0 = time.time()
            result = self.analyze(test_moves[:min(50, len(test_moves))])
            dt = time.time() - t0
            vps = v * result.num_moves / max(dt, 0.1) if result.success else 0
            results.append({"visits": v, "duration_s": round(dt, 2), "vps": round(vps, 1)})
        self.visits = original
        best = max(results, key=lambda r: r["vps"]) if results else {"visits": self.visits}
        return {"best_visits": best["visits"], "results": results}

"""WindowsAnalyzer — WSL → Windows KataGo .exe 桥接适配器。

v0.5.0: 流式常驻 KataGo 进程。
  - 单个进程跨多棋谱复用（零启动开销）
  - 逐手查询，每手超时（默认 30s）
  - 超时/死锁时杀进程重启，从该手重试
  - 每 N 局或 T 秒刷新进程（防泄漏）
"""
import json
import subprocess
import threading
import time
from typing import Optional

from .base import BaseAnalyzer, AnalysisResult, extract_12dim_features


HEARTBEAT = [["B", "pd"]]


class WindowsAnalyzer(BaseAnalyzer):
    """WSL → Windows KataGo 桥接适配器（流式常驻进程）。"""

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
        self._games = 0
        self._born = time.time()

    # ── 进程生命周期 ────────────────────────────────────

    def _start(self):
        cmd = [self.katago_path, "analysis", "-model", self.model_path]
        if self.config_path:
            cmd += ["-config", self.config_path]
        CREATE_NO_WINDOW = 0x08000000
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )

    def _kill(self):
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

    def _ensure(self) -> bool:
        """确保进程存活、健康、未超龄。"""
        now = time.time()
        restart = False
        if self._proc is None:
            restart = True
        elif self._proc.poll() is not None:
            restart = True
        elif self._games >= self.max_games:
            restart = True
        elif now - self._born >= self.max_age:
            restart = True
        if restart:
            self._kill()
            try:
                self._start()
                self._games = 0
                self._born = time.time()
            except Exception:
                self._proc = None
                return False
        return True

    # ── 单次查询（带超时） ──────────────────────────────

    def _query(self, moves: list, timeout: float) -> Optional[dict]:
        """发一条查询，读一条响应。超时返回 None。"""
        proc = self._proc
        if proc is None:
            return None
        q = json.dumps({
            "id": "q", "moves": moves,
            "maxVisits": self.visits,
            "rules": "chinese", "komi": 7.5,
            "boardXSize": 19, "boardYSize": 19,
            "includePolicy": True,
        })
        try:
            proc.stdin.write(q + "\n")
            proc.stdin.flush()
        except Exception:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = proc.stdout.readline()
                if not line:
                    return None
                r = json.loads(line.strip())
                if r.get("id") == "q":
                    return r
            except Exception:
                continue
        return None

    # ── 公开接口 ────────────────────────────────────────

    def analyze(self, moves: list) -> AnalysisResult:
        """流式分析一局棋谱。逐手查询，超时自动重启。"""
        if not moves:
            return AnalysisResult(success=True, features=[])
        n, t0 = len(moves), time.time()
        raw: dict[int, dict] = {}
        i = 0
        ok = True

        while i < n:
            with self._lock:
                if not self._ensure():
                    ok = False
                    break
                resp = self._query(moves[:i + 1], self.per_move_timeout)
            if resp is None:
                with self._lock:
                    self._kill()
                continue  # 重试同手
            mi = resp.get("moveInfos", [])
            ri = resp.get("rootInfo", {})
            raw[i] = {"id": f"g_{i}", "moveInfos": mi, "rootInfo": ri}
            i += 1

        with self._lock:
            self._games += 1

        dt = time.time() - t0
        if not raw:
            return AnalysisResult(success=False, duration_s=dt)
        feats = extract_12dim_features(raw, moves[:len(raw)])
        return AnalysisResult(features=feats, duration_s=dt, success=ok,
                              visits_used=self.visits)

    def health_check(self) -> bool:
        """健康探针。"""
        with self._lock:
            if not self._ensure():
                return False
            return self._query(HEARTBEAT, 10.0) is not None

    def shutdown(self):
        """释放 KataGo 进程。"""
        with self._lock:
            self._kill()

    def __del__(self):
        self.shutdown()

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

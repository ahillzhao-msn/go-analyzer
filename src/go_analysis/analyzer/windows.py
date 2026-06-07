"""WindowsAnalyzer — WSL → Windows KataGo .exe 桥接适配器。

v0.5.1: 常驻进程 + 批量查询。
  - 进程跨多棋谱复用（零启动开销）
  - 每局一次性发送全部查询（批处理速度）
  - 超时后杀进程重启（防死锁）
  - 每 N 局或 T 秒刷新进程
"""
import json
import subprocess
import threading
import time
from typing import Optional

from .base import BaseAnalyzer, AnalysisResult, extract_12dim_features


class WindowsAnalyzer(BaseAnalyzer):
    """WSL → Windows KataGo 桥接适配器（常驻进程 + 批量查询）。"""

    def __init__(self, katago_path: str, model_path: str,
                 config_path: Optional[str] = None,
                 visits: int = 25,
                 batch_timeout: float = 180.0,
                 max_games: int = 50,
                 max_age: float = 1800.0):
        self.katago_path = katago_path
        self.model_path = model_path
        self.config_path = config_path
        self.visits = visits
        self.batch_timeout = batch_timeout
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

    # ── 批量分析 ────────────────────────────────────────

    def analyze(self, moves: list) -> AnalysisResult:
        """批量分析一局棋谱。常驻进程复用。超时自动重启。"""
        if not moves:
            return AnalysisResult(success=True, features=[])

        n, t0 = len(moves), time.time()

        with self._lock:
            if not self._ensure():
                return AnalysisResult(success=False, duration_s=time.time() - t0)
            proc = self._proc

        # 构建全部查询
        queries = [
            json.dumps({
                "id": f"g_{i}", "moves": moves[:i],
                "maxVisits": self.visits,
                "rules": "chinese", "komi": 7.5,
                "boardXSize": 19, "boardYSize": 19,
                "includePolicy": True,
            })
            for i in range(n)
        ]

        # 发送
        try:
            proc.stdin.write("\n".join(queries) + "\n")
            proc.stdin.flush()
        except Exception:
            self._kill()
            return AnalysisResult(success=False, duration_s=time.time() - t0)

        # 读取响应（带超时）
        responses = {}
        deadline = time.time() + self.batch_timeout
        while len(responses) < n and time.time() < deadline:
            try:
                line = proc.stdout.readline()
                if not line:
                    break
                r = json.loads(line.strip())
                parts = r.get("id", "").split("_")
                if parts and parts[-1].isdigit():
                    responses[int(parts[-1])] = r
            except Exception:
                continue

        dt = time.time() - t0

        # 如果超时或读不到数据，杀进程重启
        if not responses:
            self._kill()
            return AnalysisResult(success=False, duration_s=dt)

        # 提取特征
        success = len(responses) >= n
        feats = extract_12dim_features(responses, moves[:len(responses)])

        with self._lock:
            self._games += 1

        return AnalysisResult(features=feats, duration_s=dt, success=success,
                              visits_used=self.visits)

    def shutdown(self):
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

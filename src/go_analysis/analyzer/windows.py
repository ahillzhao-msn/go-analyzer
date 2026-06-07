"""WindowsAnalyzer — WSL → Windows KataGo .exe 桥接适配器。

v0.5.1: 常驻进程 + 批量查询。
  - 进程跨多棋谱复用（零启动开销）
  - 每局一次性发送全部查询（批处理速度）
  - 超时后杀进程重启（防死锁）
  - 每 N 局或 T 秒刷新进程
  - tune()/benchmark() 是手动 API（首次部署后显式调用）
"""
import json
import logging
import subprocess
import threading
import time
from typing import Optional

from .base import BaseAnalyzer, AnalysisResult, extract_12dim_features

log = logging.getLogger("analyzer.windows")


class WindowsAnalyzer(BaseAnalyzer):
    """WSL → Windows KataGo 桥接适配器（常驻进程 + 批量查询）。"""

    def __init__(self, katago_path: str, model_path: str,
                 config_path: Optional[str] = None,
                 visits: int = 25,
                 batch_timeout: float = 180.0,
                 per_move_timeout: Optional[float] = None,
                 max_games: int = 50,
                 max_age: float = 1800.0,
                 numSearchThreads: int = 12,
                 numAnalysisThreads: int = 5,
                 nnMaxBatchSize: int = 100):
        # per_move_timeout 是旧版参数名，映射到 batch_timeout
        if per_move_timeout is not None:
            batch_timeout = per_move_timeout * 3
        self._katago_path = katago_path
        self._model_path = model_path
        self._config_path = config_path
        self.visits = visits
        self.batch_timeout = batch_timeout
        self.max_games = max_games
        self.max_age = max_age
        self.numSearchThreads = numSearchThreads
        self.numAnalysisThreads = numAnalysisThreads
        self.nnMaxBatchSize = nnMaxBatchSize

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._games = 0
        self._born = time.time()

    # ── 基类属性 ────────────────────────────────

    @property
    def katago_path(self) -> str:
        return self._katago_path

    @property
    def model_path(self) -> str:
        return self._model_path

    @property
    def config_path(self) -> Optional[str]:
        return self._config_path

    @config_path.setter
    def config_path(self, value: Optional[str]):
        self._config_path = value

    # ── 进程生命周期 ────────────────────────────

    def _start(self):
        """启动 KataGo 常驻进程。

        从 Windows Python 直接调用 exe（不需要 cmd.exe /c）。
        从 WSL Python 调用时，路径必须是 Windows 可访问的（/mnt/c/... 或 C:\...）。
        """
        cmd = [self._katago_path, "analysis", "-model", self._model_path]
        if self._config_path:
            cmd += ["-config", self._config_path]
        CREATE_NO_WINDOW = 0x08000000
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )

    def _kill(self):
        proc = getattr(self, '_proc', None)
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

    # ── 批量分析 ────────────────────────────────

    def analyze(self, moves: list) -> AnalysisResult:
        """批量分析一局棋谱。常驻进程复用。超时自动重启。"""
        if not moves:
            return AnalysisResult(success=True, features=[])

        n, t0 = len(moves), time.time()

        with self._lock:
            if not self._ensure():
                return AnalysisResult(success=False, duration_s=time.time() - t0)
            proc = self._proc

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

        try:
            proc.stdin.write("\n".join(queries) + "\n")
            proc.stdin.flush()
        except Exception:
            self._kill()
            return AnalysisResult(success=False, duration_s=time.time() - t0)

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

        if not responses:
            self._kill()
            return AnalysisResult(success=False, duration_s=dt)

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

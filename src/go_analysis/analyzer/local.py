"""LocalAnalyzer — Linux 原生 KataGo 适配器。

平台特定的 KataGo 分析器。平台无关的逻辑在 BaseAnalyzer / Pipeline 中。
"""
import json
import logging
import time
from typing import Optional

from .base import BaseAnalyzer, AnalysisResult, extract_12dim_features, moves_to_katago_format
from .process import (
    KataGoProcess, DirectProcess, _readline_with_timeout,
)

log = logging.getLogger("analyzer.local")


class LocalAnalyzer(BaseAnalyzer):
    """本地 KataGo 分析器 (Linux 原生 CUDA/OpenCL)。

    职责（纯平台相关）：
      - 用 DirectProcess 启动/管理 KataGo
      - 流式批量查询
      - 超时、重启、进程健康
    """

    def __init__(self, katago_path: str, model_path: str,
                 config_path: Optional[str] = None,
                 visits: int = 25,
                 batch_timeout: float = 180.0,
                 max_games: int = 50,
                 max_age: float = 1800.0,
                 numSearchThreads: int = 12,
                 numAnalysisThreads: int = 5,
                 nnMaxBatchSize: int = 100):
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
        self._proc: Optional[KataGoProcess] = None
        self._games = 0
        self._born = time.time()

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

    def _start(self):
        self._proc = DirectProcess()
        self._proc.start(self._katago_path, self._model_path, self._config_path)

    def _kill(self):
        proc = getattr(self, '_proc', None)
        if proc is None:
            return
        self._proc = None
        proc.kill()

    def _ensure(self) -> bool:
        now = time.time()
        restart = False
        if self._proc is None:
            restart = True
        elif not self._proc.is_alive():
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

    def analyze(self, moves: list) -> AnalysisResult:
        """批量分析一局棋谱。常驻进程复用。超时自动重启。

        Args:
            moves: [{"x": N, "y": N}, ...]  0-indexed 棋盘坐标
        """
        if not moves:
            return AnalysisResult(success=True, features=[])

        n, t0 = len(moves), time.time()
        queries = [
            json.dumps({
                "id": f"g_{i}",
                "moves": moves_to_katago_format(moves[:i]),
                "maxVisits": self.visits,
                "rules": "chinese", "komi": 7.5,
                "boardXSize": 19, "boardYSize": 19,
                "includePolicy": True,
            })
            for i in range(n)
        ]

        if not self._ensure():
            return AnalysisResult(success=False, duration_s=time.time() - t0)
        proc = self._proc
        if proc is None:
            return AnalysisResult(success=False, duration_s=time.time() - t0)

        try:
            proc.send("\n".join(queries) + "\n")
        except Exception:
            self._kill()
            return AnalysisResult(success=False, duration_s=time.time() - t0)

        responses = {}
        deadline = time.time() + self.batch_timeout
        while len(responses) < n and time.time() < deadline:
            try:
                line = _readline_with_timeout(proc, deadline)
                if line is None:
                    continue
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
        self._games += 1

        return AnalysisResult(features=feats, duration_s=dt, success=success,
                              visits_used=self.visits)

    def shutdown(self):
        self._kill()

    def __del__(self):
        self.shutdown()

"""KataGoAnalyzer — 平台无关的 KataGo 分析核心。

职责（仅此一个类）：
  1. 构建查询 JSON（moves 格式、棋盘参数）
  2. 发送批量查询到 KataGo 进程
  3. 超时读取响应
  4. 提取 12 维特征
  5. 进程生命周期管理（复用/重启）

平台差异（win/linux/WSL）委托给 KataGoProcess。
"""
import json
import logging
import time
from typing import Optional

from .base import BaseAnalyzer, AnalysisResult, extract_12dim_features, moves_to_katago_format
from .process import KataGoProcess, create_process

log = logging.getLogger("analyzer.core")


class KataGoAnalyzer(BaseAnalyzer):
    """平台无关的 KataGo 分析器。可跨多棋谱复用进程。"""

    def __init__(self, katago_path: str, model_path: str,
                 config_path: Optional[str] = None,
                 visits: int = 25,
                 batch_timeout: float = 180.0,
                 per_move_timeout: Optional[float] = None,
                 max_games: int = 50,
                 max_age: float = 1800.0,
                 process_factory=None):
        # per_move_timeout 向后兼容
        if per_move_timeout is not None:
            batch_timeout = per_move_timeout * 3
        self._katago_path = katago_path
        self._model_path = model_path
        self._config_path = config_path
        self.visits = visits
        self.batch_timeout = batch_timeout
        self.max_games = max_games
        self.max_age = max_age
        # 进程管理
        self._proc: Optional[KataGoProcess] = None
        self._games = 0
        self._born = time.time()
        self._process_factory = process_factory or create_process

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
        self._proc = self._process_factory()
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

    # ── 核心分析（平台无关）─────────────────────

    def analyze(self, moves: list) -> AnalysisResult:
        """批量分析一局棋谱。常驻进程复用。超时自动重启。

        Args:
            moves: [{"x": N, "y": N}, ...]  0-indexed 棋盘坐标

        Returns:
            AnalysisResult
        """
        if not moves:
            return AnalysisResult(success=True, features=[])

        n, t0 = len(moves), time.time()
        # 构建查询（内部{ x,y } → KataGo [[player, gtp]]）
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

        # 确保进程在运行
        if not self._ensure():
            return AnalysisResult(success=False, duration_s=time.time() - t0)
        proc = self._proc
        if proc is None:
            return AnalysisResult(success=False, duration_s=time.time() - t0)

        # 发送全部查询
        try:
            proc.send("\n".join(queries) + "\n")
        except Exception:
            if proc is not None:
                proc.kill()
            self._proc = None
            return AnalysisResult(success=False, duration_s=time.time() - t0)

        # 读取响应（带超时）
        responses = {}
        deadline = time.time() + self.batch_timeout
        while len(responses) < n and time.time() < deadline:
            try:
                line = proc.readline(deadline)
                if line is None:   # 超时，继续等
                    continue
                if not line:       # EOF
                    break
                r = json.loads(line.strip())
                parts = r.get("id", "").split("_")
                if parts and parts[-1].isdigit():
                    responses[int(parts[-1])] = r
            except Exception:
                continue

        dt = time.time() - t0

        if not responses:
            if proc is not None:
                proc.kill()
            self._proc = None
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

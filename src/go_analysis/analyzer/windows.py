"""WindowsAnalyzer — WSL → Windows KataGo .exe 桥接适配器。

纯平台适配层，所有分析逻辑委托给 KataGoAnalyzer。
"""
import logging
from typing import Optional

from .core import KataGoAnalyzer
from .process import WindowsProcess

log = logging.getLogger("analyzer.windows")


class WindowsAnalyzer(KataGoAnalyzer):
    """WSL → Windows KataGo 桥接适配器（常驻进程 + 批量查询）。

    仅比 KataGoAnalyzer 多了一个默认 process_factory=WindowsProcess。
    所有分析逻辑在 KataGoAnalyzer.analyze() 中，平台无关。
    """

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
        super().__init__(
            katago_path=katago_path, model_path=model_path,
            config_path=config_path,
            visits=visits, batch_timeout=batch_timeout,
            per_move_timeout=per_move_timeout,
            max_games=max_games, max_age=max_age,
            process_factory=WindowsProcess,
        )
        self.numSearchThreads = numSearchThreads
        self.numAnalysisThreads = numAnalysisThreads
        self.nnMaxBatchSize = nnMaxBatchSize

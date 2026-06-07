"""LocalAnalyzer — Linux 原生 KataGo 适配器。

纯平台适配层，所有分析逻辑委托给 KataGoAnalyzer。
"""
import logging
from typing import Optional

from .core import KataGoAnalyzer
from .process import DirectProcess

log = logging.getLogger("analyzer.local")


class LocalAnalyzer(KataGoAnalyzer):
    """本地 KataGo 分析器 (Linux 原生 CUDA/OpenCL)。

    仅比 KataGoAnalyzer 多了一个默认 process_factory=DirectProcess。
    所有分析逻辑在 KataGoAnalyzer.analyze() 中，平台无关。
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
        super().__init__(
            katago_path=katago_path, model_path=model_path,
            config_path=config_path,
            visits=visits, batch_timeout=batch_timeout,
            max_games=max_games, max_age=max_age,
            process_factory=DirectProcess,
        )
        self.numSearchThreads = numSearchThreads
        self.numAnalysisThreads = numAnalysisThreads
        self.nnMaxBatchSize = nnMaxBatchSize

"""
analyzer 子包 — KataGo 分析引擎的虚拟化适配层。

统一接口，多平台实现，任务调度池。

使用::

    from go_analysis.analyzer import create_adapter

    # 自动选择最佳适配器
    adapter = create_adapter()

    # 或指定平台
    adapter = create_adapter(platform="windows_native")

    result = adapter.analyze("game.sgf")
    adapter.shutdown()
"""

from .base_adapter import BaseAdapter, AnalysisResult, create_adapter
from .task import AnalysisTask, TaskState, TaskPriority
from .pool import AnalysisPool
from .adapters import WindowsNativeAdapter, SshRemoteAdapter, HttpRemoteAdapter
from .simple_analyzer import KataGoBatchAnalyzer

__all__ = [
    "BaseAdapter", "AnalysisResult",
    "AnalysisTask", "TaskState", "TaskPriority",
    "AnalysisPool",
    "WindowsNativeAdapter", "SshRemoteAdapter", "HttpRemoteAdapter",
    "create_adapter",
    "KataGoBatchAnalyzer",
]

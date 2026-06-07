"""go_analysis.analyzer — KataGo 抽象适配器

核心设计:
  - BaseAnalyzer 抽象基类定义接口契约
  - 实现: LocalAnalyzer, WindowsAnalyzer, RemoteAnalyzer
  - create_analyzer() 工厂函数根据环境自动选择实现
  - 包含 tuning/benchmark/discovery 工具

经验总结 (见 DESIGN.md 第 3.2 节):
  - per-game 独立进程比持久池可靠 (Windows OpenCL 死锁问题)
  - v1.16.x API 兼容, config/visits 参数化
  - 环境自动发现优先顺序: WSL→Windows→ENV→PATH
"""
from .base import BaseAnalyzer, AnalysisResult, create_analyzer, extract_12dim_features
from .local import LocalAnalyzer
from .windows import WindowsAnalyzer
from .streaming import StreamingWindowsAnalyzer
from .tuning import benchmark, tune, tune_gpu
from .discovery import discover_katago

__all__ = [
    "BaseAnalyzer", "AnalysisResult", "create_analyzer",
    "LocalAnalyzer", "WindowsAnalyzer", "StreamingWindowsAnalyzer",
    "benchmark", "tune", "discover_katago",
]

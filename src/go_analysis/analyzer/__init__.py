"""go_analysis.analyzer — KataGo 抽象适配器

核心设计:
  - BaseAnalyzer 抽象基类定义接口契约（analyze/shutdown/tune/benchmark）
  - KataGoAnalyzer (core.py) 平台无关的核心实现
    - 批量查询构建、超时读取、特征提取
    - 进程生命周期管理（复用/重启）
  - KataGoProcess (process.py) 平台具体的进程启动器
    - DirectProcess   — Linux/macOS 原生
    - WindowsProcess  — Windows CREATE_NO_WINDOW
  - WindowsAnalyzer / LocalAnalyzer 是薄封装（仅选默认 Process）
  - tune()/benchmark() 是手动 API（首次部署后显式调用）

经验总结:
  - KataGo Analysis Engine docs 明确 moves 格式为 [[player, gtp], ...]
  - 内部统一用 [{x, y}, ...] 格式，moves_to_katago_format() 转换
  - readline 超时：Linux 用 select，Windows 用线程池
  - nnMaxBatchSize 是 v1.16.5 强制参数（缺失即崩溃）
"""
from .base import BaseAnalyzer, AnalysisResult, create_analyzer, extract_12dim_features, moves_to_katago_format
from .core import KataGoAnalyzer
from .local import LocalAnalyzer
from .windows import WindowsAnalyzer
from .process import KataGoProcess, DirectProcess, WindowsProcess, create_process
from .tuning import (
    tune_config, benchmark as tune_benchmark,
    ConfigCandidate, TuneResult, guess_vram_mb, generate_candidates,
)
from .discovery import discover_katago, register_discovered_hosts

__all__ = [
    "BaseAnalyzer", "AnalysisResult", "create_analyzer",
    "KataGoAnalyzer", "LocalAnalyzer", "WindowsAnalyzer",
    "KataGoProcess", "DirectProcess", "WindowsProcess", "create_process",
    "tune_config", "TuneResult", "ConfigCandidate",
    "guess_vram_mb", "generate_candidates",
    "discover_katago", "register_discovered_hosts",
    "tune_benchmark", "moves_to_katago_format", "extract_12dim_features",
]

"""go_analysis.analyzer — KataGo 抽象适配器

架构:
  BaseAnalyzer                     — 平台无关的抽象接口（analyze/shutdown/tune/benchmark）
  ├── WindowsAnalyzer (windows.py) — Windows / WSL→Windows 桥接
  ├── LocalAnalyzer   (local.py)   — Linux 原生
  └── RemoteAnalyzer  (remote.py)  — SSH/HTTP 远程

  平台相关的进程管理在 process.py（Bridge 模式）：
    KataGoProcess (abstract) → DirectProcess / WindowsProcess

  调优工具在 tuning.py：
    tune_config()   — GPU 参数自动搜索
    benchmark()     — visits 基准测试
    guess_vram_mb() — GPU 显存检测

经验总结:
  - moves 格式：内部 [{x, y}] → KataGo API [[player, gtp]]
  - readline 超时：Linux select / Windows 线程池
  - tune()/benchmark() 是手动 API（首次部署后显式调用）
  - nnMaxBatchSize 是 v1.16.5 强制参数（缺失即崩溃）
"""
from .base import BaseAnalyzer, AnalysisResult, create_analyzer, extract_12dim_features, moves_to_katago_format
from .local import LocalAnalyzer
from .windows import WindowsAnalyzer
from .remote import SshRemoteAnalyzer, HttpRemoteAnalyzer
from .process import KataGoProcess, DirectProcess, WindowsProcess, create_process, _readline_with_timeout
from .tuning import (
    tune_config, benchmark as tune_benchmark,
    ConfigCandidate, TuneResult, guess_vram_mb, generate_candidates,
)
from .discovery import discover_katago, register_discovered_hosts

__all__ = [
    "BaseAnalyzer", "AnalysisResult", "create_analyzer",
    "LocalAnalyzer", "WindowsAnalyzer", "SshRemoteAnalyzer", "HttpRemoteAnalyzer",
    "KataGoProcess", "DirectProcess", "WindowsProcess", "create_process",
    "tune_config", "TuneResult", "ConfigCandidate",
    "guess_vram_mb", "generate_candidates",
    "discover_katago", "register_discovered_hosts",
    "tune_benchmark", "moves_to_katago_format", "extract_12dim_features",
]

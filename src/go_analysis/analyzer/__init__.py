"""go_analysis.analyzer — KataGo 抽象适配器

核心设计:
  - BaseAnalyzer 抽象基类定义接口契约
  - 实现: LocalAnalyzer, WindowsAnalyzer, RemoteAnalyzer
  - create_analyzer() 工厂函数根据环境自动选择实现
  - tune() 接口自动硬件调优（GPU参数 + visits）
  - benchmark() 基准测试
  - discover() 环境发现

经验总结:
  - per-game 独立进程比持久池可靠 (Windows OpenCL 死锁问题)
  - v1.16.x API 兼容, config/visits 参数化
  - nnMaxBatchSize 是 v1.16.5 强制参数（缺失即崩溃）
  - 调优: 从保守到大胆逐级测试，崩溃自动归因
"""
from .base import BaseAnalyzer, AnalysisResult, create_analyzer, extract_12dim_features
from .local import LocalAnalyzer
from .windows import WindowsAnalyzer
from .tuning import (
    tune_config, benchmark as tune_benchmark,
    ConfigCandidate, TuneResult, guess_vram_mb, generate_candidates,
)
from .discovery import discover_katago, register_discovered_hosts

__all__ = [
    "BaseAnalyzer", "AnalysisResult", "create_analyzer",
    "LocalAnalyzer", "WindowsAnalyzer",
    "tune_config", "TuneResult", "ConfigCandidate",
    "guess_vram_mb", "generate_candidates",
    "discover_katago", "register_discovered_hosts",
    "tune_benchmark",
]

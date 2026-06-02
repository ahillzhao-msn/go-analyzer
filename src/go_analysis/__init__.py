"""Go Analysis package — Transformer + Ordinal Regression for Go strength.

注意: torch/pytorch 为惰性加载, CLI 启动不受影响。
"""

# 轻量模块 — 无 torch 依赖
from .analysis_format import AnalysisRecord, AnalysisStore, HardwareEnv, SoftwareEnv, GameMeta
from .config import ConfigManager, load_config
from .sgf_parser import SGF, Move, SGFNode

def __getattr__(name):
    """惰性加载 torch 依赖模块"""
    _lazy = {}
    if name == 'GoStrengthModel':
        from .model_v2 import GoStrengthModel
        _lazy[name] = GoStrengthModel
    elif name in ('GoMoveData', 'GoGameData', 'extract_features_from_analysis', 'compute_global_stats'):
        from .models import GoMoveData, GoGameData, extract_features_from_analysis, compute_global_stats
        _lazy.update(locals())
    elif name == 'GoDataset':
        from .dataset import GoDataset
        _lazy[name] = GoDataset
    elif name == 'ModelRegistry':
        from .model_registry import ModelRegistry
        _lazy[name] = ModelRegistry
    if name in _lazy:
        return _lazy[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    'AnalysisRecord', 'AnalysisStore', 'HardwareEnv', 'SoftwareEnv', 'GameMeta',
    'ConfigManager', 'load_config',
    'SGF', 'Move', 'SGFNode',
    'GoStrengthModel', 'GoMoveData', 'GoGameData',
    'extract_features_from_analysis', 'compute_global_stats',
    'GoDataset', 'ModelRegistry',
]

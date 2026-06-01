"""Go Analysis package — Transformer + Ordinal Regression for Go strength."""

from .models import GoMoveData, GoGameData, extract_features_from_analysis, compute_global_stats
from .dataset import GoDataset
from .analysis_format import AnalysisRecord, AnalysisStore, HardwareEnv, SoftwareEnv, GameMeta
from .env_collector import collect_hardware, collect_software, extract_game_meta_from_sgf

__all__ = [
    'GoMoveData', 'GoGameData',
    'extract_features_from_analysis', 'compute_global_stats',
    'GoDataset',
    'AnalysisRecord', 'AnalysisStore', 'HardwareEnv', 'SoftwareEnv', 'GameMeta',
    'collect_hardware', 'collect_software', 'extract_game_meta_from_sgf',
]

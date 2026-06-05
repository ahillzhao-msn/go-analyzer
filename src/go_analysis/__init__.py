"""go_analysis — 围棋棋谱分析与段位评估系统。

⚠️ 注意: 此 __init__.py 只导入轻量模块 (无 torch 依赖)。
     torch 依赖的 evaluation/ 和 api/ 需显式导入:
       from go_analysis.evaluation import GoStrengthModel
       from go_analysis.api.interface import analyze_sgf

架构分层 (从下至上):
  data/        数据抽象 (source/store/format)     [pure]
  analyzer/    KataGo 适配器 (多环境自适应)       [pure]
  analysis/    SGF→NPZ 分析管线                   [pure]
  evaluation/  模型训练与推理                     [torch]
  api/         CLI + Python API                   [torch 可选]
  distributed/ 分布式框架                         [pure]
"""
from .data.format import AnalysisRecord, GameMeta, HardwareEnv, SoftwareEnv
from .data.source import BaseSource, FolderSource, SourceRegistry
from .data.store import BaseStore, NpzStore, StoreRegistry
from .analyzer import BaseAnalyzer, AnalysisResult, create_analyzer, discover_katago

__version__ = "0.3.0"

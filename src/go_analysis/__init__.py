"""go_analysis — 围棋棋谱分析与段位评估系统。

核心功能:
  1. 分析: SGF → KataGo → 12-dim feature NPZ
  2. 训练: NPZ → GoStrengthModel → 段位预测
  3. 分布式: 多主机协同分析

架构分层 (从下至上):
  data/        数据抽象 (source/store/format)
  analyzer/    KataGo 适配器 (多环境自适应)
  analysis/    SGF→NPZ 分析管线
  evaluation/  模型训练与推理 [torch]
  api/         CLI + Python API
  distributed/ 分布式框架 (coordinator/worker/sync/deploy)
"""
from .api import analyze_sgf, evaluate_game, train_model, discover
from .data.format import AnalysisRecord, GameMeta, HardwareEnv, SoftwareEnv
from .data.source import BaseSource, FolderSource, SourceRegistry
from .data.store import BaseStore, NpzStore, StoreRegistry

__version__ = "0.2.0"

"""go_analysis.api — 统一接口层。

提供 CLI (Click) 和 Python Package 两种调用方式。

使用方式:
  Python:   from go_analysis import analyze, evaluate, train
  CLI:      go-analyzer analyze game.sgf --visits 25
            go-analyzer train --store ./analysis_store --epochs 50
            go-analyzer cluster start --port 18081
"""
from .interface import analyze_sgf, evaluate_game, train_model, discover

__all__ = ["analyze_sgf", "evaluate_game", "train_model", "discover"]

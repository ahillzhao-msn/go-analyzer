"""go_analysis.data — 数据抽象层 (pure Python, 无 torch 依赖)

三层结构:
  source/    BaseSource → FolderSource | YunyiSource | ...
  store/     BaseStore  → NpzStore | SqlStore | ...
  format.py  数据契约 (AnalysisRecord, 12-dim feature)
"""

# Go Analyzer

围棋棋力分析系统 — Transformer + 序数回归 (Ordinal Regression)

## 架构

```
sgf/ ──→ KataGoBatchAnalyzer (原始 JSON)
         │
         ▼
   analysis_format.py
   ├── 12维落子特征矩阵 [T×12] float16
   ├── 硬件环境向量 (CPU/GPU/RAM/VRAM)
   ├── 软件环境向量 (CUDA/Torch/KataGo版本+配置)
   └── 棋谱环境向量 (规则/komi/选手/让子/总步数/时间)
         │  (.npz 压缩存储, ~5KB/局)
         ▼
   dataset.py ──→ model.py ──→ train.py ──→ calibrate.py
```

## 模块

| 模块 | 功能 |
|------|------|
| `models.py` | 12维 per-move 特征 + 12维全局统计 + KataGo JSON → 特征向量 |
| `analyzer.py` | KataGo 子进程包装器，批量分析 SGF 棋谱 |
| `analysis_format.py` | **压缩记录格式** — npz 二进制存储 + GameMeta 归一化 |
| `env_collector.py` | 三大环境向量采集 (硬件/软件/棋谱) |
| `dataset.py` | PyTorch Dataset，可变长度序列 + padding |
| `model.py` | Transformer Encoder + Ordinal Logistic Head |
| `train.py` | 训练循环，QWK 监控，checkpoint |
| `calibrate.py` | Label Distribution Matching 段位校准 |

## 12 维特征

| # | 名称 | 说明 |
|---|------|------|
| 0 | `top1_hit` | 玩家是否下在 KataGo 推荐第1位 |
| 1 | `top5_hit` | 玩家是否下在 KataGo 推荐前5 |
| 2 | `complexity` | 1 - max(policy)，棋局复杂度 |
| 3 | `policy_entropy` | 全策略分布熵 |
| 4 | `prior` | KataGo 先验概率 |
| 5 | `winrate` | KataGo 胜率估计 |
| 6 | `score_lead` | KataGo 预期领先分数 |
| 7 | `score_stdev` | 分数标准差 (不确定性) |
| 8 | `utility` | KataGo 综合效用值 |
| 9 | `lcb` | 下限置信度 |
| 10 | `avg_visits` | 该手访问次数占比 |
| 11 | `player` | 黑=0, 白=1 |

## GameMeta 归一化

所有棋谱字段经过严格归一化:

| 字段 | 原始格式 | 归一化后 |
|------|---------|---------|
| rank | `5段`, `5D`, `9d` | `5d`, `9d` |
| result | `W+RESIGN`, `W+Time`, `0` | `W+R`, `W+T`, `B+R` |
| date | `20160315`, `2016.3.5`, `2016-03-15a` | `2016-03-15` |
| rules | `JP`, `CN`, `Japanese` | `japanese`, `chinese` |
| time | `600/1/60`, `30 1` | `600`, `30 1` |

## 使用

```bash
# 1. 批分析 + 压缩
python -c "
from go_analysis import KataGoBatchAnalyzer, AnalysisStore, AnalysisRecord
from go_analysis.env_collector import collect_hardware, collect_software, extract_game_meta_from_sgf_file

hw = collect_hardware()
sw = collect_software()
store = AnalysisStore('analysis_results/')
analyzer = KataGoBatchAnalyzer(katago_path='/path/to/katago')

for sgf_path in ['game1.sgf', 'game2.sgf']:
    result = analyzer.analyze_sgf_file(sgf_path)
    if result:
        from go_analysis.models import extract_features_from_analysis, compute_global_stats
        moves = extract_features_from_analysis(result, 'B')
        feats = [m.features for m in moves]
        if feats:
            features = np.stack(feats, axis=0)
            gs = compute_global_stats(moves)
            game = extract_game_meta_from_sgf_file(sgf_path)
            rec = AnalysisRecord.compress(features, gs, hw, sw, game)
            store.put(game.game_id, rec)

analyzer.shutdown()
print(store.stats())
"

# 2. 训练
python -m go_analysis.train --data analysis_results/ --epochs 50
```

## 安装

```bash
pip install -e .
```

需安装 [KataGo](https://github.com/lightvector/KataGo/releases) 并下载模型权重。

# Go Analyzer v2 — 架构设计

> 一条拳谱贯穿全局：**SGF → Analyze → NPZ → Train → Evaluate**
> 所有层遵循单向依赖，工厂模式隐藏实现细节。

---

## 一、核心工作流

```
[SGF 来源]
    │
    ▼
data/source (BaseSource)     ← 抽象化棋谱来源
    │
    ▼
analysis/pipeline            ← 调用 analyzer 完成分析
    │
    ▼
data/store (BaseStore)       ← 抽象化存储后端
    │
    ├── ▶ evaluation/train    ← 训练评估模型
    ├── ▶ evaluation/inference ← 推理段位评级
    └── ▶ distributed/sync   ← 分布式结果同步
```

## 二、架构层次

```
┌─────────────────────────────────────────────────────────┐
│  api/          CLI / Python API                         │
│  distributed/  分布式分析框架 (协调器+工人+同步+部署)     │
├─────────────────────────────────────────────────────────┤
│  evaluation/  评估模型 (训练 + 推理)              [torch]│
│  analysis/    SGF→NPZ 分析管线                    [pure]│
│  analyzer/    KataGo 抽象适配器 (多环境自适应)    [pure]│
├─────────────────────────────────────────────────────────┤
│  data/        数据抽象层 (source/store/format)   [pure]  │
└─────────────────────────────────────────────────────────┘
```

**依赖规则**（箭头方向即依赖方向）：

```
api ─→ distributed ─→ evaluation ─→ analysis ─→ analyzer ─→ data
                    ↘ analysis ────────────────────→ analyzer ─→ data
                    所有层均可使用 data/ (纯 Python 层)
```

**无 torch 依赖**：`data/`, `analyzer/`, `analysis/` 不依赖 PyTorch，可部署到无 GPU 主机。

---

## 三、模块详解

### 3.1 data/ — 数据抽象层 (pure Python)

#### Source — 棋谱来源抽象

```python
class BaseSource(ABC):
    def list_games() -> list[str]       # 返回所有 game_id
    def get_game(id) -> (sgf_str, meta) # 获取 SGF 内容 + 元数据
    def count() -> int                  # 棋谱总数
    def exists(id) -> bool              # 检查是否存在
```

实现：
- `FolderSource` — 本地文件夹 (递归扫描 `*.sgf`)
- `YunyiSource` — Yunyi 平台 HTTP API (将来扩展)
- `DatabaseSource` — SQL 数据库 (将来扩展)

#### Store — 分析结果存储抽象

```python
class BaseStore(ABC):
    def save(id, record: AnalysisRecord)  # 保存分析结果
    def load(id) -> AnalysisRecord        # 读取分析结果
    def list() -> list[str]              # 返回所有已保存 game_id
    def exists(id) -> bool               # 检查是否存在
```

实现：
- `NpzStore` — 文件系统 `*.npz` 存储（当前使用）
- `SqlStore` — SQL 数据库 (将来扩展)
- `RemoteStore` — HTTP 远程存储 (将来扩展)

#### Format — 数据契约 (AnalysisRecord)

单一 NPZ 文件包含：

```
features:  ndarray (N×12)  12维 move 特征矩阵
metadata:  dict             棋谱元数据结果
hardware_env: dict           硬件环境采集
software_env: dict           软件环境采集
```

12-dim move feature vector:

| # | 字段 | 说明 |
|---|------|------|
| 0 | is_best | 该手是否被 KataGo 选为最佳 (order==0) |
| 1 | is_top5 | 该手是否在 top5 候选内 (order<5) |
| 2 | 1 - max_policy | 策略网络对该手的置信度倒数 |
| 3 | entropy | 策略网络输出熵 (局面复杂度) |
| 4 | prior | 策略网络先验概率 |
| 5 | winrate | 胜率 (KataGo 评估) |
| 6 | score_lead | 领先目数 |
| 7 | score_stdev | 目数标准差 (不确定性) |
| 8 | utility | KataGo 效用值 |
| 9 | lcb | 下置信边界 |
| 10 | visits_ratio | 该手访问次数 / 总访问次数 |
| 11 | side | 执黑=0 / 执白=1 |

---

### 3.2 analyzer/ — KataGo 抽象适配器

```python
class BaseAnalyzer(ABC):
    def analyze(moves: list) -> AnalysisResult  # 核心方法
    def benchmark() -> dict                      # 基准测试
    def tune() -> dict                           # 参数调优
    def discover() -> dict                       # 自动发现环境
```

实现：
- `LocalAnalyzer` — WSL/Linux 原生 (CUDA/OpenCL)
- `WindowsAnalyzer` — WSL→Windows .exe 桥接
- `RemoteAnalyzer` — SSH/HTTP 远程主机

**关键经验**（见 ROADMAP.md 第 4 节）：

| 问题 | 解决方案 |
|------|---------|
| KataGo 持久进程死锁 | per-game 独立启动进程更可靠 (虽 3-5s 启动开销) |
| 不同版本兼容 | v1.16.x API 兼容，config/visits 参数化 |
| Windows 路径 | 使用 WSL interop path (`/mnt/c/..`) |
| OpenCL 配置 | 需要 `analysis_config.cfg` |

---

### 3.3 analysis/ — SGF→NPZ 分析管线

```python
class Pipeline:
    def __init__(analyzer: BaseAnalyzer, source: BaseSource, store: BaseStore)
    
    def run_one(game_id: str) -> bool          # 分析单谱
    def run_all() -> dict[str, bool]           # 分析全部
    def resume() -> dict[str, bool]            # 从中断处继续
```

核心逻辑：SGF → `extract_main_line()` → moves → `analyzer.analyze(moves)` → 12-dim features → 采集环境 → 封装 `AnalysisRecord` → `store.save()`。

**主线提取**：分支点只取 `children[0]`（第一条变化），忽略复盘评论、变化图。

**验证规则**：最小 50 手（不足标记 skip，非失败），避免死循环。

---

### 3.4 evaluation/ — 评估模型 (torch)

```python
class Evaluator:
    def __init__(store: BaseStore)
    
    def train() -> dict                    # 训练模型
    def evaluate(game_id) -> dict          # 评估单谱
    def evaluate_batch(game_ids) -> dict   # 批量评估
```

模型架构：GoStrengthModel v2 — 黑白分离 Causal Self-Attention，12-dim→128→2×6-head→128→64→1 (ordinal regression)。

输出：
- Black rank / White rank（段位评级）
- Confidence interval
- Per-move contribution

---

### 3.5 distributed/ — 分布式分析框架

三组件协作：

```
              ┌──────────────┐
              │  Coordinator │  ← 任务调度 (HTTP stdlib)
              └──────┬───────┘
                     │ assign/complete
            ┌────────┴────────┐
            ▼                  ▼
       ┌─────────┐      ┌─────────┐
       │ Worker1 │ ...  │ WorkerN │
       │(WSL)   │      │(WORKER_HOST)│
       └─────────┘      └─────────┘
```

#### Coordinator
- `POST /register` — Worker 注册（自报已有结果）
- `GET /assign` — 获取下一个未完成 SGF
- `POST /complete` — 报告任务完成（成功/skip/失败）
- `GET /stats` — 全量状态统计

#### Worker
- 循环: register → assign → analyze → complete
- 本地 `store/analysis/analyzer` 自持运行
- 注册时自报 `store_dir` + `done_games` 避免重复

#### Sync — 结果同步层

```python
class ResultSync:
    def deploy_sgfs(source, worker_path) -> int    # 部署 SGF 到 worker 本地
    def pull_results(worker_info, central_store) -> int  # 拉取结果回中心
    def verify(coordinator_url) -> dict             # 校验完整性
```

**核心原则**：Worker 完全离线运行（零运行时依赖中心），同步是后置的、幂等的。

#### Deployment — 一键部署

保留经验：
- UV/Poetry build → pip install 分发
- PowerShell 脚本模板（SCHTASKS 72h 定时任务）
- 防火墙规则自动化
- KataGo 自动发现 + 调优

---

### 3.6 api/ — 接口层

```python
# Python API
from go_analysis_v2 import analyze, evaluate, train

result = analyze(
    sgf_content="...",
    analyzer="windows",
    katago_path="./katago.exe",
    model_path="./model.bin.gz"
)  # → AnalysisRecord

rank = evaluate(
    record=result,
    model_path="./model.pt"
)  # → EvaluationResult

# CLI
$ go-analyzer analyze game.sgf --visits 25
$ go-analyzer evaluate game.npz --model model.pt
$ go-analyzer train --store ./analysis_store --epochs 50
$ go-analyzer cluster start --port 18081
```

---

## 四、数据流 (完整示例)

```python
# 创建一个完整分析管道
source = FolderSource("./training")
store = NpzStore("./analysis_store")
analyzer = create_analyzer("windows", kata_path="./katago.exe", model="./model.bin.gz")
pipeline = Pipeline(analyzer, source, store)

# 逐谱分析
stats = pipeline.run_all()
print(f"Done: {stats['success']}, Skipped: {stats['skip']}, Failed: {stats['failed']}")

# 训练模型
evaluator = create_evaluator(store)
train_result = evaluator.train()
print(f"Best epoch: {train_result['best_epoch']}, Val loss: {train_result['val_loss']}")

# 推理
game_result = evaluator.evaluate("001-rabbit-jansteen-jon")
print(f"Black: {game_result['black_rank']}, White: {game_result['white_rank']}")
```

## 五、分布式工作流

```python
# 协调器端
coord = Coordinator(sgf_source=source, analysis_store=central_store)
coord.start(host="0.0.0.0", port=18081)

# Worker 端 (在远程主机上)
worker = Worker(
    coordinator_url="http://COORDINATOR_IP:18081",
    worker_id="WORKER_HOST",
    analyzer=create_analyzer("windows", ...),
    store=NpzStore("./analysis_store"),
    sgf_source=FolderSource("./training"),
)
worker.run()  # 循环: register → assign → analyze → complete

# 同步
sync = ResultSync(central_store=central_store)
n = sync.pull_results(worker_info={"workdir": r"C:\Users\WORKER_USER\go-analyzer-worker"})
print(f"Synced {n} results from WORKER_HOST")

# 校验
report = sync.verify(coordinator_url="http://localhost:18081")
print(report)  # {"total": 8003, "synced": 8003, "missing": []}
```

---

## 六、设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 依赖管理 | UV + pyproject.toml | 经验证可靠，多环境一致 |
| KataGo 进程模型 | per-game 独立启动 | 持久进程在 Windows OpenCL 上死锁 |
| 通信协议 | HTTP + JSON (stdlib) | 零额外依赖 |
| 存储格式 | NPZ (单一文件) | 自包含，易传输，numpy 原生 |
| SGF 来源抽象 | BaseSource 接口 | 支持 folder/DB/Yunyi/将来扩展 |
| 存储抽象 | BaseStore 接口 | 支持 file/SQL/remote |
| Worker 模式 | pull (轮询) | push 需要 center 知道 worker 地址 |
| 同步策略 | 后置幂等同步 | Worker 可离线运行 |

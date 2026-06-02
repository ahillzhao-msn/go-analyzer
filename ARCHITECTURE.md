# Go Analyzer — 架构白皮书

> 以道为心（抽象），法为骨（接口），儒为皮（配置），兵为用（部署）。

---

## 一、现有架构审计

```
src/go_analysis/
├── __init__.py          # 包入口
├── main.py              # ⬜ 空 — CLI 入口待实现
├── util.py              # ⬜ 空 — 工具函数待填充
│
├── analyzer/            # 🔑 虚拟化分析引擎层
│   ├── __init__.py      #   导出: create_adapter(), BaseAdapter, 各类适配器
│   ├── base_adapter.py  #   Abstract BaseAdapter + AnalysisResult + create_adapter()
│   ├── protocol.py      #   AnalysisProtocol: SGF↔moves, 查询构建, 坐标转换
│   ├── task.py          #   AnalysisTask: 任务状态机 (PENDING→RUNNING→DONE/FAILED)
│   ├── pool.py          #   AnalysisPool: 多进程调度池 + 优先级 + 并发控制
│   ├── simple_analyzer.py # 旧版直接子进程分析器 (pipeline 仍引用)
│   └── adapters/
│       ├── __init__.py
│       ├── windows_native.py  # Windows OpenCL KataGo (WSL interop)
│       ├── ssh_remote.py      # SSH 远程主机适配器
│       └── http_remote.py     # HTTP REST 远程适配器
│
├── sgf_parser.py        # 📄 SGF 解析引擎 (SGF→树→Move列表)
├── analysis_format.py   # 📦 数据格式: AnalysisRecord, AnalysisStore, GameMeta
├── dataset.py           # 📦 数据集工具: GoDataset 类
│
├── models.py            # 🧠 数据模型: GoMoveData, GoGameData
├── model.py             # 🧠 神经网络: GoStrengthModel (v2)
├── model_v1_archive.py  # 🧠 旧版模型 (存档)
├── model_v2.py          # 🧠 新版模型 (与 model.py 同?)
├── train.py             # 🏋️ 训练循环: NPZDataset, train_epoch, validate
│
├── calibrate.py         # 📐 输出校准: LabelCalibrator (线性映射 f(x)=ax+b)
├── go_predict.py        # 🔮 预测接口: GoPredict (推理管线)
│
├── env_collector.py     # 🔍 环境收集: hardware/software info
├── pipeline.py          # 🔄 端到端分析管线: meta + full 模式
│
├── worker_server.py     # 🌐 远程 worker HTTP 服务
├── yunyi.py             # (待确认用途)
└── main.py              # ⬜ 空
```

### 优点

| 层面 | 现有能力 | 评价 |
|------|---------|------|
| 适配器模式 | BaseAdapter → 3 个平台实现 | ✅ 架构正确，接口统一 |
| 工厂模式 | `create_adapter(platform=...)` | ✅ 调用方无需关心实现 |
| 协议封装 | AnalysisProtocol 处理 SGF/moves/坐标 | ✅ KataGo 版本兼容 |
| 任务调度 | AnalysisTask + AnalysisPool + 优先级 | ✅ 完整 |
| 分布式 | SshRemoteAdapter + HttpRemoteAdapter | ✅ 基础框架就绪 |
| 数据格式 | AnalysisStore (文件 JSON) | ✅ 可扩展 |
| 训练 | 离线批量训练 + loss/val 循环 | ✅ |
| 校准 | LabelCalibrator 线性映射 | ✅ |

### 缺口

| 层面 | 缺失 | 影响 |
|------|------|------|
| **配置管理** | 无 ConfigManager，路径硬编码 | ❌ 不可移植 |
| **CLI 入口** | `main.py` 空 | ❌ 无法 CLI 调用 |
| **Host 注册** | 远程适配器需手动配置地址 | ❌ 无自动发现 |
| **健康检查** | 无 host 心跳/状态监控 | ❌ 可靠性 |
| **模型导出** | 无 export/deploy 流程 | ❌ 部署断链 |
| **增量训练** | 仅离线全量训练 | ❌ 不能持续学习 |
| **测试** | 无集成/单元测试 | ❌ 回归风险 |
| **online inference** | 需与分析流程耦合方可实时推理 | ⚠️ |

---

## 二、目标架构

```
                    ┌──────────────────────┐
                    │     ConfigManager     │  ← pyproject.toml / YAML
                    │   (配置注册/覆盖/持久) │
                    └──────────┬───────────┘
                               │
┌──────────────────────────────┼──────────────────────────────┐
│                     CLI (main.py)                            │
│  analyze │ train │ predict │ serve │ register │ export       │
└──────────────────────────────┼──────────────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  AnalysisRouter  │  │   TrainingPipe  │  │  ModelRegistry  │
│  (分析路由调度器)  │  │  (训练管线)      │  │  (模型注册表)    │
├─────────────────┤  ├─────────────────┤  ├─────────────────┤
│ • HostRegistry   │  │ • BatchTrain    │  │ • Export/Import  │
│ • AdapterPool    │  │ • Incremental   │  │ • Versioning    │
│ • TaskScheduler   │  │ • Resume        │  │ • Deployment   │
│ • HealthMonitor  │  │ • Eval/Validate │  │ • Onnx/Torch   │
│ • AutoDiscovery  │  │ • Configurable  │  │ • A/B Test     │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │                     │                     │
         ▼                     ▼                     ▼
┌──────────────────────────────────────────────────────────┐
│                     AnalysisStore                         │
│              (分析存储: 文件 → PostgreSQL)                   │
└──────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│                    Adapter Layer                          │
│  (create_adapter → WindowsNative / SSHRouter / HTTPRouter) │
└──────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│               KataGo Engine Cluster                       │
│  [本地 WSL]  [Host_01]  [Host_02]  [Host_03]  ...        │
└──────────────────────────────────────────────────────────┘
```

---

## 三、核心组件设计

### 3.1 ConfigManager — 配置管理器

```python
# 设计目标: 单例模式, 支持多层覆盖
# 优先级: CLI 参数 > 环境变量 > YAML 配置 > 默认值

class ConfigManager:
    """配置管理器 — 全局单例"""
    
    _instance = None
    
    def __init__(self):
        self._config = self._load_defaults()
        self._load_from_file()     # pyproject.toml / config.yaml
        self._load_from_env()      # GO_ANALYZER_*
    
    def get(self, key: str, default=None):
        """支持点号路径: config.get('analyzer.visits')"""
        
    def set(self, key: str, value):
        """运行时覆盖"""
        
    def freeze(self):
        """锁定配置 (训练开始后不可变)"""
```

**配置项设计** (YAML):

```yaml
analyzer:
  default_platform: windows_native  # auto | windows_native | ssh | http
  visits: 96
  num_analysis_threads: 2
  num_search_threads: 16
  nn_max_batch_size: 8

storage:
  backend: file  # file | postgres | sqlite
  path: ./analysis_store
  compress: true

hosts:
  - name: office-gpu
    platform: windows_native
    path: C:/kata-go/katago.exe
    model: kata1-b18c384nbt-s6582191360-d3422816034.bin.gz
    max_concurrent: 3
    heartbeat: 30

  - name: cloud-worker-1
    platform: ssh
    host: worker1.example.com
    port: 22
    kata_path: /opt/katago/katago
    key: ~/.ssh/worker_key
    max_concurrent: 2

model:
  name: go-strength-v2
  input_dim: 12
  hidden_dim: 256
  num_heads: 4
  num_layers: 3
  num_ranks: 9  # ordinal classes

training:
  batch_size: 32
  learning_rate: 0.001
  epochs: 100
  early_stopping: 10
  incremental: false  # true = 在已有模型上继续训练
  resume_checkpoint: null
```

### 3.2 AnalysisRouter — 分析路由调度器

```python
class AnalysisRouter:
    """
    分析路由调度器 — 管理和调度所有注册的 KataGo 分析主机。
    
    功能:
    1. Host 注册/注销 (自发现 + 手动注册)
    2. 健康检查 (心跳 + 探针)
    3. 负载感知调度 (最少连接 / 最快响应 / 轮询)
    4. 动态扩缩容
    5. 任务中断恢复
    """
    
    def __init__(self, config: ConfigManager):
        self._hosts: dict[str, HostRecord] = {}  # name → HostRecord
        self._tasks: AnalysisPool                  # 复用现有池
        self._monitor: HealthMonitor
    
    def register_host(self, host: HostConfig) -> str:
        """注册分析主机 → 返回 host_id"""
        
    def unregister_host(self, host_id: str):
        """注销主机, 迁移未完成任务"""
        
    def discover_hosts(self, network: str = "192.168.1.0/24"):
        """广播发现局域网内的 worker 节点"""
        
    def analyze(self, sgf_content: str, visits: int = None,
                priority: int = 0, host_id: str = None) -> str:
        """提交分析任务 → 返回 task_id"""
        
    def analyze_batch(self, sgfs: list, **kwargs) -> list[str]:
        """批量提交"""
        
    def get_result(self, task_id: str, block: bool = False) -> AnalysisResult:
        """获取结果 (可选阻塞)"""
        
    def get_status(self) -> RouterStatus:
        """路由器状态: 主机数/任务数/负载"""
```

```python
@dataclass
class HostRecord:
    """分析主机注册记录"""
    id: str
    name: str
    platform: str          # windows_native | ssh | http
    adapter: BaseAdapter   # 已初始化的适配器实例
    max_concurrent: int
    current_tasks: int
    capabilities: set      # {'opencl', 'cuda', 'cpu'}
    health: HostHealth
    last_heartbeat: float
    registered_at: float
```

### 3.3 HealthMonitor — 健康监控

```python
class HealthMonitor(threading.Thread):
    """后台心跳监控线程"""
    
    CHECK_INTERVAL = 15  # 秒
    
    def add_host(self, host_id: str, check_fn: Callable):
    def remove_host(self, host_id: str):
    def get_healthy_hosts(self) -> list[str]:
    def is_healthy(self, host_id: str) -> bool:
    
    def run(self):
        while self._running:
            for host_id, check_fn in self._hosts.items():
                if not check_fn():
                    self._mark_unhealthy(host_id)
                    self._trigger_failover(host_id)
            time.sleep(self.CHECK_INTERVAL)
```

### 3.4 TrainingPipe — 训练管线

```python
class TrainingPipe:
    """
    训练管线 — 支持离线全量 + 在线增量训练。
    
    流程:
    1. load_analysis(store)    从 AnalysisStore 加载特征
    2. prepare_dataset()       构建 NPZDataset
    3. train()                 全量训练 或 增量微调
    4. evaluate()              验证集评估
    5. calibrate()             输出校准 (LabelCalibrator)
    6. export()                导出为部署格式 (ONNX / TorchScript)
    """
    
    MODES = ['full', 'incremental', 'resume']
    
    def __init__(self, config: ConfigManager):
        self.model: GoStrengthModel
        self.dataset: GoDataset
        self.store: AnalysisStore
    
    def train(self, mode: str = 'full'):
        """训练入口"""
        
    def incremental(self, new_data: list[AnalysisRecord]):
        """增量训练 — 在新数据上微调已有模型"""
        
    def export(self, format: str = 'onnx', path: str = None):
        """导出模型"""
```

### 3.5 ModelRegistry — 模型注册表

```python
class ModelRegistry:
    """
    模型注册表 — 管理模型的版本、部署、回滚。
    
    目录结构:
    models/
    ├── v1/
    │   ├── model.pt          # PyTorch 权重
    │   ├── config.yaml        # 训练配置
    │   ├── metrics.json       # 评估指标
    │   └── calibrator.pkl     # 校准器
    ├── v2/ ...
    └── latest -> v2           # 当前最新
    """
    
    def save(self, name: str, model, metrics: dict, config: dict):
    def load(self, version: str = "latest") -> GoStrengthModel:
    def list_versions(self) -> list[VersionInfo]:
    def rollback(self, version: str):
    def deploy(self, version: str, target: str = "local"):
```

---

## 四、分析流程重构方案

### 4.1 当前问题

`pipeline.py` 硬编码引用 `KataGoBatchAnalyzer`（旧版简单分析器），而非虚拟化适配器层。

```python
# 当前 pipeline.py 第 152 行:
analyzer = KataGoBatchAnalyzer(        # ← 直接实例化, 绕过适配器
    katago_path=katago_path,
    model_path=model_path,
    ...
)
```

### 4.2 目标重构

```python
# 目标 pipeline.py:
from go_analysis.analyzer import create_adapter
from go_analysis.config import ConfigManager

config = ConfigManager()
adapter = create_adapter(
    platform=config.get('analyzer.default_platform'),
    visits=config.get('analyzer.visits'),
)

# 通过路由器调度
router = AnalysisRouter(config)
router.register_host('local', adapter)
task_id = router.analyze(sgf_content, visits=25)
result = router.get_result(task_id, block=True)
```

### 4.3 支持的 CLI 接口

```bash
# 1. 分析单盘
go-analyzer analyze game.sgf --visits 96 --output result.json

# 2. 批量分析
go-analyzer analyze-batch ./sgf_dir/ --mode full --visits 50

# 3. 注册远程主机
go-analyzer host register --name worker-1 --platform ssh \
    --host 192.168.1.100 --kata-path /opt/katago/katago

# 4. 查看集群状态
go-analyzer cluster status

# 5. 训练
go-analyzer train --mode full --epochs 100 --lr 0.001

# 6. 增量训练
go-analyzer train --mode incremental --checkpoint v2 --new-data ./new_games/

# 7. 导出模型
go-analyzer export --version v3 --format onnx --output ./deploy/

# 8. 启动 worker 服务 (供路由器注册)
go-analyzer serve --port 8080 --kata-path ./kata-go/windows/katago.exe
```

---

## 五、实施路线图

### Phase 1: 基础设施 (1-2 天)
| 任务 | 文件 | 说明 |
|------|------|------|
| ConfigManager | `config.py` | YAML + ENV + CLI 三层覆盖 |
| CLI 入口 | `main.py` | click/argparse 路由 |
| ModelRegistry | `model_registry.py` | 版本管理 + 导出 |
| 分析流程重构 | `pipeline.py` | 切到 Adapter + Router |

### Phase 2: 主机管理 (2-3 天)
| 任务 | 文件 | 说明 |
|------|------|------|
| HostRegistry | `host_registry.py` | 注册/发现/状态 |
| HealthMonitor | `health.py` | 心跳 + 探针 + 故障转移 |
| Host auto-discovery | `discovery.py` | UDP 广播 / mDNS |
| AdapterPool | 增强 `pool.py` | 多主机负载均衡 |

### Phase 3: 训练增强 (2-3 天)
| 任务 | 文件 | 说明 |
|------|------|------|
| TrainingPipe | `training_pipe.py` | 统一训练入口 |
| 增量训练 | 增强 `train.py` | checkpoint 加载 + 冻结层 |
| 模型导出 | 新增 `export.py` | ONNX / TorchScript |

### Phase 4: 存储扩展 (1-2 天)
| 任务 | 文件 | 说明 |
|------|------|------|
| AnalysisStore 抽象 | `analysis_format.py` | 接口化 (file→SQL) |
| PostgreSQL 后端 | `store_pg.py` | 可选 |
| SQLite 后端 | `store_sqlite.py` | 轻量替代 |

---

## 六、关键架构决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 配置格式 | YAML | 可读性好, 支持注释, Python 原生解析 |
| CLI 框架 | `click` | 比 argparse 更声明式, 支持嵌套命令 |
| 模型导出 | ONNX 优先 | 跨平台部署, PyTorch→ONNX→TensorRT |
| 远程协议 | HTTP REST + WebSocket | 通用, 防火墙友好, WS 支持流式 |
| 主机发现 | mDNS (Avahi) | 零配置局域网发现 |
| 任务存储 | AnalysisStore 抽象 + SQLAlchemy | 文件→SQL 无痛迁移 |

---

> 大道至简。架构的最终目标是让调用者只需:
> ```python
> from go_analysis import GoAnalyzer
> analyzer = GoAnalyzer()
> result = analyzer.analyze("game.sgf")
> model = analyzer.train(training_data)
> analyzer.export(model, "deploy/model.onnx")
> ```

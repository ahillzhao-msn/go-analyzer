# ROADMAP — 实施路线图 & 经验沉淀

> 本文件记录所有经过验证的工作流、已知问题和关键决策，**避免在新画板上重复试错**。

---

## 1. 项目初始化 (已验证)

### 1.1 环境

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### 1.2 打包与发布

```bash
uv build              # 生成 wheel
uv pip install dist/*.whl  # 目标主机安装
```

**路径经验**：`-e .` 开发安装 vs `dist/*.whl` 发布安装。远程主机用 wheel 部署。

### 1.3 pyproject.toml 配置结构

```toml
[project]
name = "go-analyzer-v2"
version = "0.2.0"
dependencies = [
    "numpy>=1.24",
    "click>=8.0",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
torch = ["torch>=2.0", "tqdm>=4.0"]
analysis = ["torch"]  # evaluation 需要 torch
dev = ["pytest", "build"]

[project.scripts]
go-analyzer = "go_analysis_v2.api.cli:cli"
```

---

## 2. 部署到远程主机 (已验证)

### 2.1 目录约定

远程主机根目录结构 (如 bob-pc: `C:\Users\WORKER_USER\go-analyzer-worker\`):

```
go-analyzer-worker/
├── training/              # SGF 棋谱 (由 coordinator 部署)
│   ├── 1d-3d/ (2400)
│   ├── 30k-10k/ (800)
│   ├── 4d-6d/ (1600)
│   ├── 7d-9d/ (800)
│   ├── 9k-1k/ (1603)
│   └── pro/ (800)
├── analysis_store/        # 分析结果 NPZ
├── katago/                # KataGo 二进制 + 模型
│   ├── katago.exe
│   ├── analysis_config.cfg
│   └── model.bin.gz
├── .venv/                 # Python 虚拟环境
└── logs/                  # 运行日志
```

### 2.2 Windows 防火墙

```powershell
netsh advfirewall firewall add rule name=GoAnalyzer \
    dir=in action=allow protocol=TCP localport=18081
```

### 2.3 SCHTASKS 定时任务 (保持 Worker 存活)

```powershell
schtasks /create /tn GoAgent /tr "C:\Users\WORKER_USER\go-analyzer-worker\agent.bat" \
    /sc onstart /ru SYSTEM /rl HIGHEST /f
schtasks /change /tn GoAgent /ri 10 /du 72:00
```

### 2.4 agent.bat 模板

```batch
@echo off
cd /d %WORKDIR%
call .venv\Scripts\activate.bat
python -m go_analysis_v2.distributed.worker ^
    --coordinator http://COORDINATOR_IP:18081 ^
    --worker-id HOSTNAME ^
    --sgf-dir ./training ^
    --store-dir ./analysis_store ^
    --katago ./katago/katago.exe ^
    --model ./katago/model.bin.gz ^
    --config ./katago/analysis_config.cfg ^
    --visits 25 --min-moves 10
```

---

## 3. 网络 & SSH (已验证)

### 3.1 WSL → Windows 通信

```bash
# WSL IP (固定)
COORDINATOR_IP

# 从 WSL 访问 Windows KataGo
/path/to/project/.katrain/KataGo/katago.exe

# 从 WSL 访问 Windows 文件
/mnt/c/Users/WORKER_USER/go-analyzer-worker/
```

### 3.2 SSH 到 Windows (bob-pc)

```bash
ssh REMOTE_USER@REMOTE_HOST_IP
# 密码认证。机器可能休眠 → 首次 ping 超时正常
# Windows CMD 命令: dir, tasklist, schtasks, wmic
```

### 3.3 HTTP 通信 (Worker ↔ Coordinator)

```python
# stdlib 实现, 无 Flask/Django
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
```

---

## 4. KataGo 适配 (核心经验)

### 4.1 版本兼容

| 环境 | 版本 | 后端 | 路径 |
|------|------|------|------|
| WSL (开发机) | v1.16.4 | OpenCL | `/path/to/project/.katrain/KataGo/katago.exe` |
| WSL (cuDNN) | v1.16.4 | CUDA | 同上 (不同 .exe) |
| bob-pc | v1.16.5 | OpenCL | `C:\Users\WORKER_USER\go-analyzer-worker\katago\katago.exe` |

### 4.2 进程模型

```
❌ 持久进程池 (pool.py):
   EngineInstance → Popen(katago) → 健康检查 → TCP fallback
   问题: v1.16.5 OpenCL 存在暂态阻塞, 持久 2-3小时后死锁

✅ per-game 独立进程 (推荐):
   for game in games:
       proc = Popen([katago, "analysis", "-model", model, "-config", config])
       proc.stdin.write(queries)
       proc.stdin.close()
       proc.wait(120)
   # 开销: 3-5s/局 启动时间, 但 10/10 零失败
```

### 4.3 KataGo 查询格式

```json
{
    "id": "g_0",
    "moves": [["B", "Q16"], ["W", "D4"]],
    "maxVisits": 25,
    "rules": "chinese",
    "komi": 7.5,
    "boardXSize": 19,
    "boardYSize": 19,
    "includePolicy": true
}
```

### 4.4 环境自动发现

```python
def discover_katago():
    """返回可用 KataGo 路径列表 (优先级排序)。"""
    # 1. WSL → Windows KataGo
    # 2. WSL 原生 KataGo
    # 3. 环境变量 KATAGO_PATH
    # 4. PATH 中的 katago
```

### 4.5 基准测试 + 参数调优

```python
def benchmark(katago_path, model_path, test_sgf, visits_range=[50, 100, 200, 400]):
    """找出最优 visits/配置组合。"""
```

---

## 5. SGF 处理 (核心经验)

### 5.1 解析规则

- 只提取 **主线** (first child at each branch)
- 弃掉变化图、复盘评论
- 最小 50 手 → 不足标记 `skip`
- 19×19, HA=0 校验

### 5.2 已知陷阱

| 问题 | 表现 | 原因 | 修复 |
|------|------|------|------|
| `\\\\w+` 正则 | 解析出 0 手 | 双反斜杠在字符串中被解释为单反斜杠 | 统一用 `go_analysis_v2.data.sgf_parser` |
| `[tt]` 表示 pass | 解析为坐标 `(19,19)` | 某些服务器习惯 | SGF 坐标等于 board_size 时判 pass |

---

## 6. 数据契约 (NPZ 格式)

### 6.1 文件命名

```
{game_id}.npz            # 完整分析结果
{game_id}.meta.npz       # 元数据 (环境信息)
```

### 6.2 存储字段

```python
np.savez(
    "game.npz",
    features=features,        # ndarray (N, 12)
    game_id=game_id,          # str
    black_rank=br,            # int
    white_rank=wr,            # int
    move_count=n,             # int
    black_player=bp,          # str
    white_player=wp,          # str
    komi=komi,                # float
    result=result,            # str
    visits=visits,            # int
    hardware_env=json.dumps(hw),  # str
    software_env=json.dumps(sw),  # str
)
```

---

## 7. Worker 注册 & 去重机制

```python
# Worker 启动时:
POST /register {
    "worker_id": "bob-pc",
    "store_dir": "C:/Users/WORKER_USER/go-analyzer-worker/analysis_store",
    "done_games": ["001-xxx", "002-yyy", ...]  # 扫描本地 store 得到
}

# Coordinator 端:
known_done = merge(
    all_workers.done_games,   # Worker 自报
    scan(store_dir.npz),      # 本地 Store 扫描
)
remaining = total_sgfs - len(known_done) - assigned
```

---

## 8. 功能核对清单 (实施顺序)

| 阶段 | 模块 | 依赖 | 预计工作量 |
|------|------|------|-----------|
| 1 | `data/` 层 | 无 | ⭐⭐ |
| 2 | `analyzer/` 层 | data/ | ⭐⭐⭐ |
| 3 | `analysis/` 管线 | data/, analyzer/ | ⭐⭐ |
| 4 | `evaluation/` 模型 | data/ | ⭐⭐⭐⭐ |
| 5 | `api/` CLI | 以上全部 | ⭐ |
| 6 | `distributed/` | 以上全部 | ⭐⭐⭐ |
| 7 | 集成测试 | 以上全部 | ⭐⭐ |
| 8 | 替换主版本 | 测试通过 | ⭐ |

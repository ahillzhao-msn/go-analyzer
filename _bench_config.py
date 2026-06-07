"""
Benchmark: 测试不同 numSearchThreads × numAnalysisThreads × nnMaxBatchSize 组合。
方法：对每个配置，启动 KataGo analysis 进程，发送 50 手 25 vis 的查询，测速+稳定性。
"""
import json, subprocess, time, os, sys

# Paths (WSL/Linux paths)
WORKER_DIR = "/mnt/c/users/xiaoj/go-analyzer-worker"
KATAGO_EXE = os.path.join(WORKER_DIR, "katago", "katago.exe")
MODEL = os.path.join(WORKER_DIR, "models", "kata1-b18c384nbt-s6582191360-d3422816034.bin.gz")

# Windows paths for cmd.exe
KATAGO_WIN = r"C:\Users\xiaoj\go-analyzer-worker\katago\katago.exe"
MODEL_WIN = r"C:\Users\xiaoj\go-analyzer-worker\models\kata1-b18c384nbt-s6582191360-d3422816034.bin.gz"

# 标准50手开局
STARTER_MOVES = [
    {"x": 3, "y": 3}, {"x": 15, "y": 15}, {"x": 3, "y": 15}, {"x": 15, "y": 3},
    {"x": 3, "y": 9}, {"x": 15, "y": 9}, {"x": 9, "y": 3}, {"x": 9, "y": 15},
    {"x": 6, "y": 5}, {"x": 12, "y": 13}, {"x": 5, "y": 12}, {"x": 13, "y": 6},
    {"x": 7, "y": 2}, {"x": 11, "y": 16}, {"x": 2, "y": 11}, {"x": 16, "y": 7},
    {"x": 10, "y": 5}, {"x": 8, "y": 13}, {"x": 4, "y": 7}, {"x": 14, "y": 11},
    {"x": 8, "y": 2}, {"x": 10, "y": 16}, {"x": 2, "y": 8}, {"x": 16, "y": 10},
    {"x": 9, "y": 7}, {"x": 9, "y": 11}, {"x": 7, "y": 9}, {"x": 11, "y": 9},
    {"x": 5, "y": 5}, {"x": 13, "y": 13}, {"x": 5, "y": 13}, {"x": 13, "y": 5},
    {"x": 10, "y": 3}, {"x": 8, "y": 15}, {"x": 3, "y": 10}, {"x": 15, "y": 8},
    {"x": 6, "y": 9}, {"x": 12, "y": 9}, {"x": 9, "y": 6}, {"x": 9, "y": 12},
    {"x": 7, "y": 6}, {"x": 11, "y": 12}, {"x": 6, "y": 11}, {"x": 12, "y": 7},
    {"x": 4, "y": 4}, {"x": 14, "y": 14}, {"x": 4, "y": 14}, {"x": 14, "y": 4},
    {"x": 9, "y": 5}, {"x": 9, "y": 13},
]

TOTAL_MOVES = min(len(STARTER_MOVES), 50)


def make_config(search_threads, analysis_threads, batch_size):
    return f"""reportAnalysisWinratesAs = BLACK
analysisPVLen = 15
wideRootNoise = 0.04
numAnalysisThreads = {analysis_threads}
numSearchThreads = {search_threads}
nnMaxBatchSize = {batch_size}
"""


def test_config(search_threads, analysis_threads, batch_size, visits=25, timeout=120):
    """启动KataGo，发送50手查询，返回 (success, time_s, vps, error)"""
    config_text = make_config(search_threads, analysis_threads, batch_size)

    # 写临时配置（WSL路径，Windows exe通过 /mnt/c 也能读到）
    cfg_wsl = os.path.join(WORKER_DIR, "_bench_cfg.cfg")
    with open(cfg_wsl, "w") as f:
        f.write(config_text)

    cfg_win = r"C:\Users\xiaoj\go-analyzer-worker\_bench_cfg.cfg"
    cmd = ["cmd.exe", "/c", KATAGO_WIN, "analysis",
           "-model", MODEL_WIN, "-config", cfg_win]

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
    except Exception as e:
        return False, 0, 0, f"启动失败: {e}"

    # 制造查询
    n = TOTAL_MOVES
    queries = [
        json.dumps({
            "id": f"q_{i}", "moves": STARTER_MOVES[:i],
            "maxVisits": visits,
            "rules": "chinese", "komi": 7.5,
            "boardXSize": 19, "boardYSize": 19,
            "includePolicy": False,
        })
        for i in range(n)
    ]

    t0 = time.time()
    try:
        stdin_data = "\n".join(queries) + "\n"
        proc.stdin.write(stdin_data)
        proc.stdin.flush()
    except Exception as e:
        proc.kill()
        os.remove(cfg_wsl)
        return False, time.time() - t0, 0, f"stdin写入失败: {e}"

    # 读取响应（带超时）
    responses = {}
    deadline = time.time() + timeout
    while len(responses) < n and time.time() < deadline:
        try:
            line = proc.stdout.readline()
            if not line:
                break
            r = json.loads(line.strip())
            parts = r.get("id", "").split("_")
            if parts and parts[-1].isdigit():
                responses[int(parts[-1])] = r
        except Exception:
            continue

    dt = time.time() - t0

    # 检查是否崩溃
    rc = proc.poll()
    if rc is not None and rc != 0:
        stderr = proc.stderr.read()[:300] if proc.stderr else ""
        proc.kill()
        os.remove(cfg_wsl)
        return False, dt, 0, f"崩溃 exit={rc}: {stderr[:150]}"

    if not responses:
        proc.kill()
        os.remove(cfg_wsl)
        return False, dt, 0, "零响应（可能是崩溃或超时）"

    received = len(responses)
    vps = received * visits / max(dt, 0.1)

    proc.terminate()
    try:
        proc.wait(3)
    except Exception:
        proc.kill()

    os.remove(cfg_wsl)
    return True, round(dt, 2), round(vps, 1), f"{received}/{n} moves"


# ── Phase 1: 用户假设值主测试 ──
print("=" * 70)
print("Phase 1 — 用户假设值测试")
print(f"  50 moves, 25 visits each")
print(f"  Model: kata1-b18c384nbt")
print(f"  GPU: RTX3060 12GB (OpenCL)")
print("=" * 70)

configs_p1 = [
    (10, 5, 50,  "用户假设: search=10, analysis=5, batch=50"),
    (10, 5, 32,  "batch=32"),
    (10, 5, 64,  "batch=64"),
    (10, 5, 100, "batch=100"),
]

for st, at, bs, label in configs_p1:
    sys.stdout.write(f"[{label}] ")
    sys.stdout.flush()
    success, dt, vps, info = test_config(st, at, bs)
    if success:
        sys.stdout.write(f" ✅ {dt}s, {vps} vps ({info})\n")
    else:
        sys.stdout.write(f" ❌ {info}\n")
    sys.stdout.flush()

# ── Phase 2: 固定 batch=50, 变量 analysis_threads ──
print("\n" + "=" * 70)
print("Phase 2 — 固定 batch=50, 变量 analysis_threads")
print("=" * 70)

configs_p2 = [
    (10, 2, 50, "analysis=2"),
    (10, 4, 50, "analysis=4"),
    (10, 5, 50, "analysis=5 (用户假设)"),
    (10, 6, 50, "analysis=6"),
    (10, 8, 50, "analysis=8"),
]

for st, at, bs, label in configs_p2:
    sys.stdout.write(f"[{label}] ")
    sys.stdout.flush()
    success, dt, vps, info = test_config(st, at, bs)
    if success:
        sys.stdout.write(f" ✅ {dt}s, {vps} vps ({info})\n")
    else:
        sys.stdout.write(f" ❌ {info}\n")
    sys.stdout.flush()

# ── Phase 3: 固定 batch=50 + best analysis, 变量 search_threads ──
print("\n" + "=" * 70)
print("Phase 3 — 固定 batch=50 + best analysis, 变量 search_threads")
print("=" * 70)

configs_p3 = [
    (6,  5, 50, "search=6"),
    (8,  5, 50, "search=8"),
    (10, 5, 50, "search=10 (用户假设)"),
    (12, 5, 50, "search=12"),
    (14, 5, 50, "search=14"),
]

for st, at, bs, label in configs_p3:
    sys.stdout.write(f"[{label}] ")
    sys.stdout.flush()
    success, dt, vps, info = test_config(st, at, bs)
    if success:
        sys.stdout.write(f" ✅ {dt}s, {vps} vps ({info})\n")
    else:
        sys.stdout.write(f" ❌ {info}\n")
    sys.stdout.flush()

# ── Phase 4: 确认最佳组合下的 visits 性能 ──
print("\n" + "=" * 70)
print("Phase 4 — 最佳组合下变量 visits")
print("=" * 70)

for v in [25, 50, 100, 200]:
    sys.stdout.write(f"[visits={v}] ")
    sys.stdout.flush()
    success, dt, vps, info = test_config(10, 5, 64, visits=v)
    if success:
        sys.stdout.write(f" ✅ {dt}s, {vps} vps ({info})\n")
    else:
        sys.stdout.write(f" ❌ {info}\n")
    sys.stdout.flush()

print("\n" + "=" * 70)
print("Done.")
print("=" * 70)

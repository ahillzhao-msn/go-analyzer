"""Tuning — 安全自动调优 KataGo GPU 参数。

核心逻辑：
  1. 根据硬件（GPU 显存、CPU 核数）生成安全参数候选
  2. 从最保守到最大胆逐级测试
  3. 检测崩溃并归因（nnMaxBatchSize 缺失、显存不足等）
  4. 返回最优配置 + 生成推荐配置文件
  5. 跨平台：支持 WSL→Windows 桥接
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .base import moves_to_katago_format


# ── 数据结构 ────────────────────────────────────────

@dataclass
class ConfigCandidate:
    """单一配置候选。"""
    numSearchThreads: int
    numAnalysisThreads: int
    nnMaxBatchSize: int
    label: str = ""


@dataclass
class TuneResult:
    """单次测试结果。"""
    config: ConfigCandidate
    duration_s: float
    vps: float
    moves_analyzed: int
    success: bool
    error: str = ""


# ── 硬件感知参数生成 ────────────────────────────────

def guess_vram_mb() -> Optional[int]:
    """检测 GPU 显存（MB）。保守估计优先。"""
    # 方法1: nvidia-smi
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            v = int(r.stdout.strip().split("\n")[0].strip())
            return v
    except Exception:
        pass

    # 方法2: Windows nvidia-smi (WSL 调用)
    try:
        r = subprocess.run(
            ["cmd.exe", "/c", "nvidia-smi", "--query-gpu=memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            v = int(r.stdout.strip().split("\n")[0].strip())
            return v
    except Exception:
        pass

    # 方法3: 已知 GPU 型号推测
    try:
        if sys.platform == "win32":
            # Windows: wmic
            r = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                capture_output=True, text=True, timeout=5,
            )
            name = r.stdout.lower()
        else:
            # Linux/WSL: lspci
            r = subprocess.run(
                ["lspci"], capture_output=True, text=True, timeout=5,
            )
            name = r.stdout.lower()

        if "rtx 4090" in name or "rtx 4080" in name:
            return 16384
        if "rtx 3090" in name:
            return 24576
        if "rtx 3080" in name:
            return 12288
        if "rtx 3070" in name:
            return 8192
        if "rtx 4060" in name:
            return 8192
        if "rtx 3060" in name:
            return 12288
        if "rtx 2070" in name:
            return 8192
        if "rtx 2060" in name:
            return 6144
        if "gtx 1660" in name:
            return 6144
        if "gtx 1080" in name:
            return 8192
        if "gtx 1070" in name:
            return 8192
        if "gtx 1060" in name:
            return 6144
    except Exception:
        pass

    return None


def generate_candidates(vram_mb: Optional[int] = None) -> list[ConfigCandidate]:
    """根据显存生成安全的参数候选列表（从保守到大胆）。"""
    cpu_cores = os.cpu_count() or 8

    if vram_mb and vram_mb >= 16384:
        # 16GB+ (RTX 4090/4080/3090/3080)
        return [
            ConfigCandidate(6,  2, 16,  "保守"),
            ConfigCandidate(8,  4, 32,  "适中"),
            ConfigCandidate(10, 5, 64,  "进取"),
            ConfigCandidate(12, 6, 100, "大胆"),
            ConfigCandidate(min(cpu_cores, 16), 8, 128, "极限"),
        ]
    elif vram_mb and vram_mb >= 8192:
        # 8GB (RTX 3070/4060/2070/1080)
        return [
            ConfigCandidate(6,  2, 16,  "保守"),
            ConfigCandidate(8,  4, 32,  "适中"),
            ConfigCandidate(10, 5, 50,  "进取"),
            ConfigCandidate(12, 6, 64,  "大胆"),
        ]
    elif vram_mb and vram_mb >= 4096:
        # 4-6GB (RTX 2060/GTX 1660/1060)
        return [
            ConfigCandidate(4,  1, 8,   "保守"),
            ConfigCandidate(6,  2, 16,  "适中"),
            ConfigCandidate(8,  4, 32,  "进取"),
            ConfigCandidate(min(cpu_cores, 12), 6, 50, "大胆"),
        ]
    else:
        # 未知/CPU
        return [
            ConfigCandidate(2,  1, 4,   "保守"),
            ConfigCandidate(4,  2, 8,   "适中"),
            ConfigCandidate(min(cpu_cores, 8), 4, 16, "进取"),
        ]


# ── 测试执行 ────────────────────────────────────────

# 50 手标准测试棋谱
_STARTER_MOVES = [
    ({"x": 3, "y": 3}, {"x": 15, "y": 15}, {"x": 3, "y": 15}, {"x": 15, "y": 3},
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
     {"x": 9, "y": 5}, {"x": 9, "y": 13}),
][0]

_N_TEST_MOVES = 50


def _build_cfg(
    base_lines: list[str],
    candidate: ConfigCandidate,
) -> str:
    """构建临时配置文本。"""
    # 去掉可能冲突的旧参数行
    filtered = [
        l for l in base_lines
        if l.strip() and not any(
            l.strip().lower().startswith(k)
            for k in ["numsearchthreads", "numanalysisthreads", "nnmaxbatchsize"]
        )
    ]
    filtered += [
        f"numSearchThreads = {candidate.numSearchThreads}",
        f"nnMaxBatchSize = {candidate.nnMaxBatchSize}",
        f"numAnalysisThreads = {candidate.numAnalysisThreads}",
    ]
    return "\n".join(filtered) + "\n"


def _test_candidate(
    katago_path: str,
    model_path: str,
    config_text: str,
    visits: int = 25,
    timeout: float = 120.0,
    use_cmd_shell: bool = False,
) -> TuneResult:
    """测试单个配置候选。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="katago_tune_"))
    cfg_path = tmpdir / "test.cfg"
    cfg_path.write_text(config_text)

    # 构造命令
    if use_cmd_shell:
        # WSL → Windows exe: 用 cmd.exe /c 桥接
        cmd = ["cmd.exe", "/c", katago_path, "analysis",
               "-model", model_path, "-config", str(cfg_path)]
    else:
        cmd = [katago_path, "analysis", "-model", model_path, "-config", str(cfg_path)]

    moves = _STARTER_MOVES
    n = _N_TEST_MOVES
    queries = [
        json.dumps({
            "id": f"q_{i}", "moves": moves_to_katago_format(moves[:i]),
            "maxVisits": visits,
            "rules": "chinese", "komi": 7.5,
            "boardXSize": 19, "boardYSize": 19,
            "includePolicy": False,  # 降低 I/O 开销
        })
        for i in range(n)
    ]

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            cwd=str(tmpdir),
        )
    except Exception as e:
        shutil.rmtree(str(tmpdir), ignore_errors=True)
        return TuneResult(
            ConfigCandidate(0, 0, 0), 0, 0, 0, False,
            f"启动失败: {e}",
        )

    t0 = time.time()
    try:
        stdin_data = "\n".join(queries) + "\n"
        proc.stdin.write(stdin_data)
        proc.stdin.flush()
    except Exception as e:
        proc.kill()
        shutil.rmtree(str(tmpdir), ignore_errors=True)
        return TuneResult(
            ConfigCandidate(0, 0, 0), time.time() - t0, 0, 0, False,
            f"stdin写入失败: {e}",
        )

    # 读取响应
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

    # 崩溃诊断
    rc = proc.poll()
    if rc is not None and rc != 0:
        stderr_text = ""
        try:
            stderr_text = proc.stderr.read()[:500]
        except Exception:
            pass

        # 归因
        error_msg = f"崩溃 exit={rc}"
        stderr_lower = stderr_text.lower()
        if "command line error" in stderr_lower or "no such option" in stderr_lower:
            error_msg += " (命令行参数错误)"
        elif "nnmaxbatchsize" in stderr_lower:
            error_msg += " (nnMaxBatchSize 缺失，KataGo v1.16.5+ 强制要求)"
        elif "out of memory" in stderr_lower or "cuda error" in stderr_lower:
            error_msg += " (显存不足)"
        elif "opencl" in stderr_lower:
            error_msg += f" (OpenCL 错误, stderr={stderr_text[:200]})"
        else:
            error_msg += f" (stderr={stderr_text[:200]})"

        proc.kill()
        shutil.rmtree(str(tmpdir), ignore_errors=True)
        return TuneResult(
            ConfigCandidate(0, 0, 0), dt, 0, 0, False, error_msg,
        )

    if not responses:
        proc.kill()
        shutil.rmtree(str(tmpdir), ignore_errors=True)
        return TuneResult(
            ConfigCandidate(0, 0, 0), dt, 0, 0, False, "零响应（可能是超时或死锁）",
        )

    # 成功
    received = len(responses)
    vps = received * visits / max(dt, 0.1)

    proc.terminate()
    try:
        proc.wait(3)
    except Exception:
        proc.kill()

    shutil.rmtree(str(tmpdir), ignore_errors=True)

    return TuneResult(
        candidate, round(dt, 2), round(vps, 1),
        received, True,
    )


# ── 主入口 ──────────────────────────────────────────

def tune_config(
    katago_path: str,
    model_path: str,
    base_config_path: Optional[str] = None,
    visits: int = 25,
    candidates: Optional[list[ConfigCandidate]] = None,
    output_path: Optional[str] = None,
) -> dict:
    """GPU 参数调优 — 安全逐级测试，返回最优配置。

    流程：
      1. 检测硬件显存 → 生成候选参数列表（保守→大胆）
      2. 每个候选逐级测试（先跑保守的，崩溃立即停止）
      3. 崩溃归因（nnMaxBatchSize 缺失、显存不足等）
      4. 返回最优配置 + 可选写入推荐 config 文件

    Args:
        katago_path: KataGo 可执行文件路径或 Windows/WSL 路径
        model_path:  模型文件路径
        base_config_path: 基础配置模板（可选，会保留其中的 reportAnalysisWinratesAs 等）
        visits:      测试用的 visits 值
        candidates:  自定义候选列表（自动生成时传 None）
        output_path: 推荐配置文件写入路径（可选）

    Returns:
        {
            "best_config": {numSearchThreads, numAnalysisThreads, nnMaxBatchSize, vps},
            "results": [TuneResult 列表],
            "diagnosis": "最佳" | "保守" | "不稳定",
            "recommended_config_path": str or "",
            "vram_mb": int or None,
        }
    """
    # 1. 硬件检测
    vram_mb = guess_vram_mb()

    # 2. 生成候选
    if candidates is None:
        candidates = generate_candidates(vram_mb)

    # 3. 读取基础配置
    base_lines = []
    if base_config_path:
        cfg = Path(base_config_path)
        if cfg.exists():
            base_lines = [
                l.rstrip("\n\r")
                for l in cfg.read_text(encoding="utf-8", errors="replace").split("\n")
                if l.strip() and not l.strip().startswith("#")
                and "logDir" not in l
            ]

    # 4. 检测是否需要 cmd.exe（WSL→Windows）
    use_cmd = False
    if sys.platform == "linux":
        try:
            r = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=3)
            if "microsoft" in r.stdout.lower() or "wsl" in r.stdout.lower():
                use_cmd = True
        except Exception:
            pass
    elif sys.platform == "win32":
        use_cmd = False  # 直接调用

    # 5. 逐级测试
    results = []
    best = None

    for cand in candidates:
        cfg_text = _build_cfg(base_lines, cand)
        r = _test_candidate(
            katago_path, model_path, cfg_text,
            visits=visits, use_cmd_shell=use_cmd,
        )
        # 把候选参数回填（_test_candidate 可能建了空 ConfigCandidate）
        r.config = cand
        results.append(r)

        if r.success:
            if best is None or r.vps > best.vps:
                best = r
        else:
            # 崩溃 → 停止更激进的测试
            break

    # 6. 诊断
    if best is None:
        diagnosis = "不稳定"
        # 保底：返回第一个候选（全失败，但至少结构完整）
        best = results[0] if results else TuneResult(
            candidates[0] if candidates else ConfigCandidate(4, 2, 8),
            0, 0, 0, False,
        )
    elif best == results[0]:
        diagnosis = "保守"
    elif best == results[-1]:
        diagnosis = "最佳"
    else:
        diagnosis = "最佳"

    # 7. 写入推荐配置文件
    recommended_path = ""
    if output_path and best.success:
        try:
            out = Path(output_path)
            # 保留基础配置中的非冲突行
            out_lines = base_lines + [
                f"numSearchThreads = {best.config.numSearchThreads}",
                f"nnMaxBatchSize = {best.config.nnMaxBatchSize}",
                f"numAnalysisThreads = {best.config.numAnalysisThreads}",
                f"# 自动调优: vps={best.vps}, vram={vram_mb}MB",
            ]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(out_lines) + "\n")
            recommended_path = str(out)
        except Exception as e:
            recommended_path = f"写入失败: {e}"

    return {
        "best_config": {
            "numSearchThreads": best.config.numSearchThreads,
            "numAnalysisThreads": best.config.numAnalysisThreads,
            "nnMaxBatchSize": best.config.nnMaxBatchSize,
            "vps": best.vps,
        },
        "results": [
            {
                "numSearchThreads": r.config.numSearchThreads,
                "numAnalysisThreads": r.config.numAnalysisThreads,
                "nnMaxBatchSize": r.config.nnMaxBatchSize,
                "duration_s": r.duration_s,
                "vps": r.vps,
                "success": r.success,
                "error": r.error,
                "label": r.config.label,
            }
            for r in results
        ],
        "diagnosis": diagnosis,
        "recommended_config_path": recommended_path,
        "vram_mb": vram_mb,
    }


def benchmark(
    katago_path: str,
    model_path: str,
    test_sgf_or_moves,
    visits_range: list = None,
    config_path: Optional[str] = None,
) -> dict:
    """standalone 基准测试 — 找出最优 visits 配置（兼容旧接口）。

    Args:
        katago_path: KataGo 可执行文件路径
        model_path: 模型文件路径
        test_sgf_or_moves: SGF 文件路径 或 moves list
        visits_range: 要测试的 visits 值列表
        config_path: KataGo 配置文件路径

    Returns:
        {"best_visits": N, "best_vps": X, "results": [{visits, duration_s, vps, ...}]}
    """
    if visits_range is None:
        visits_range = [25, 50, 100, 200]

    # 解析输入
    if isinstance(test_sgf_or_moves, (str, Path)):
        # SGF 文件
        try:
            from ..analysis.sgf_parser import extract_main_line
            content = Path(test_sgf_or_moves).read_text(encoding="utf-8", errors="replace")
            moves = extract_main_line(content)[:50]
        except Exception:
            moves = _STARTER_MOVES
    else:
        moves = test_sgf_or_moves[:50] if test_sgf_or_moves else _STARTER_MOVES

    if not moves:
        return {"error": "No moves", "best_visits": 25, "results": []}

    # 读取基础配置
    base_lines = []
    if config_path:
        cfg = Path(config_path)
        if cfg.exists():
            base_lines = [
                l.rstrip("\n\r")
                for l in cfg.read_text(encoding="utf-8", errors="replace").split("\n")
                if l.strip() and not l.strip().startswith("#") and "logDir" not in l
            ]

    # 检测是否需要 cmd.exe
    use_cmd = False
    if sys.platform == "linux":
        try:
            r = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=3)
            if "microsoft" in r.stdout.lower() or "wsl" in r.stdout.lower():
                use_cmd = True
        except Exception:
            pass

    results = []
    for v in visits_range:
        t0 = time.time()
        try:
            cfg_text = _build_cfg(base_lines, ConfigCandidate(8, 4, 32))
            r = _test_candidate(
                katago_path, model_path, cfg_text,
                visits=v, use_cmd_shell=use_cmd,
            )
            dt = time.time() - t0
            results.append({
                "visits": v,
                "duration_s": round(r.duration_s, 2),
                "vps": r.vps,
                "moves": r.moves_analyzed,
                "success": r.success,
            })
        except Exception:
            results.append({"visits": v, "success": False})

    best = max(results, key=lambda r: r.get("vps", 0)) if results else {}
    return {"best_visits": best.get("visits", 25), "best_vps": best.get("vps", 0), "results": results}

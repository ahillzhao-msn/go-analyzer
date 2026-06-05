"""Tuning — KataGo 基准测试与参数调优。"""
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class BenchmarkResult:
    """基准测试结果。"""
    visits: int
    duration_s: float
    vps: float           # visits per second
    moves_analyzed: int
    success: bool
    config: str = "default"


def benchmark(
    katago_path: str,
    model_path: str,
    test_sgf_path: str,
    visits_range: list = None,
    config_path: Optional[str] = None,
) -> dict:
    """运行基准测试，找出最优 visits 配置。

    Args:
        katago_path: KataGo 可执行文件路径
        model_path: 模型文件路径
        test_sgf_path: 测试棋谱路径 (用于提取位移)
        visits_range: 要测试的 visits 值列表
        config_path: KataGo 配置文件路径

    Returns:
        包含所有测试结果的 dict
    """
    if visits_range is None:
        visits_range = [25, 50, 100, 200, 400]

    # 提取位移
    from ..analysis.sgf_parser import extract_main_line
    content = Path(test_sgf_path).read_text(encoding="utf-8", errors="replace")
    moves = extract_main_line(content)[:50]  # 只测试前 50 手
    if not moves:
        return {"error": "No moves in test SGF", "best_visits": 25}

    results = []
    for visits in visits_range:
        t0 = time.time()
        try:
            cmd = [katago_path, "analysis", "-model", model_path]
            if config_path:
                cmd += ["-config", config_path]

            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            queries = [
                json.dumps({
                    "id": f"g_{i}", "moves": moves[:i],
                    "maxVisits": visits,
                    "rules": "chinese", "komi": 7.5,
                    "boardXSize": 19, "boardYSize": 19,
                    "includePolicy": True,
                })
                for i in range(len(moves))
            ]
            proc.stdin.write("\n".join(queries) + "\n")
            proc.stdin.flush()

            resp_count = 0
            while resp_count < len(moves):
                line = proc.stdout.readline()
                if not line:
                    break
                resp_count += 1

            proc.stdin.close()
            proc.terminate()
            proc.wait(3)
            dt = time.time() - t0
            vps = visits * len(moves) / max(dt, 0.1)
            results.append({"visits": visits, "duration_s": round(dt, 2),
                           "vps": round(vps, 1), "success": True,
                           "moves": len(moves)})
        except Exception:
            results.append({"visits": visits, "success": False})

    best = max(results, key=lambda r: r.get("vps", 0)) if results else {}
    return {"best_visits": best.get("visits", 25), "results": results}


def tune_gpu(
    katago_path: str,
    model_path: str,
    test_sgf_path: str,
    base_config_path: Optional[str] = None,
    visits: int = 50,
) -> dict:
    """GPU 参数调优 — 测试不同 nnMaxBatchSize / numAnalysisThreads 组合。

    生成临时 KataGo 配置，测试各组合的性能 (visits/sec)，
    返回最优参数推荐。

    Args:
        katago_path: KataGo 可执行文件路径
        model_path: 模型文件路径
        test_sgf_path: 测试棋谱路径
        base_config_path: 基础配置模板路径
        visits: 测试用的 visits 值

    Returns:
        {"best_config": {...}, "results": [{...}], "recommended_config_path": str}
    """
    import tempfile, shutil

    # 1. 采集硬件环境, 动态选择测试参数
    try:
        from ..analysis.environment import collect_hardware
        hw = collect_hardware()
    except Exception:
        hw = None

    gpu_mem = None
    if hw and hw.gpu_memory_mb:
        gpu_mem = hw.gpu_memory_mb

    cpu_cores = os.cpu_count() or 8

    # 根据 GPU 显存选择参数范围
    if gpu_mem and gpu_mem >= 8192:          # 8GB+ (RTX 3060 12GB)
        test_params = [
            {"numSearchThreads": 8, "nnMaxBatchSize": 8, "numAnalysisThreads": 2},
            {"numSearchThreads": 16, "nnMaxBatchSize": 16, "numAnalysisThreads": 4},
            {"numSearchThreads": 16, "nnMaxBatchSize": 32, "numAnalysisThreads": 6},
            {"numSearchThreads": 20, "nnMaxBatchSize": 64, "numAnalysisThreads": 8},
            {"numSearchThreads": min(cpu_cores, 20), "nnMaxBatchSize": 128, "numAnalysisThreads": 10},
        ]
    elif gpu_mem and gpu_mem >= 4096:         # 4-8GB (GTX 1660 Ti 6GB)
        test_params = [
            {"numSearchThreads": 4, "nnMaxBatchSize": 4, "numAnalysisThreads": 1},
            {"numSearchThreads": 8, "nnMaxBatchSize": 8, "numAnalysisThreads": 2},
            {"numSearchThreads": 16, "nnMaxBatchSize": 16, "numAnalysisThreads": 4},
            {"numSearchThreads": 16, "nnMaxBatchSize": 32, "numAnalysisThreads": 6},
            {"numSearchThreads": min(cpu_cores, 16), "nnMaxBatchSize": 64, "numAnalysisThreads": 6},
        ]
    else:                                      # 未知/CPU-only
        test_params = [
            {"numSearchThreads": 2, "nnMaxBatchSize": 2, "numAnalysisThreads": 1},
            {"numSearchThreads": 4, "nnMaxBatchSize": 4, "numAnalysisThreads": 2},
            {"numSearchThreads": 8, "nnMaxBatchSize": 8, "numAnalysisThreads": 4},
            {"numSearchThreads": min(cpu_cores, 12), "nnMaxBatchSize": 8, "numAnalysisThreads": 4},
        ]

    # 2. 读取基础配置
    base_lines = []
    if base_config_path:
        base = Path(base_config_path)
        if base.exists():
            base_lines = [l for l in base.read_text().split("\n")
                         if l.strip() and not l.strip().startswith("logDir")]

    # 测试参数组合
    test_params = [
        {"numSearchThreads": 4, "nnMaxBatchSize": 4, "numAnalysisThreads": 1},
        {"numSearchThreads": 8, "nnMaxBatchSize": 8, "numAnalysisThreads": 2},
        {"numSearchThreads": 16, "nnMaxBatchSize": 16, "numAnalysisThreads": 4},
        {"numSearchThreads": 16, "nnMaxBatchSize": 32, "numAnalysisThreads": 6},
        {"numSearchThreads": 16, "nnMaxBatchSize": 64, "numAnalysisThreads": 8},
    ]

    results = []
    tmpdir = Path(tempfile.mkdtemp(prefix="katago_tune_"))

    for params in test_params:
        # 生成临时配置
        cfg_lines = list(base_lines) + [
            f"numSearchThreads = {params['numSearchThreads']}",
            f"nnMaxBatchSize = {params['nnMaxBatchSize']}",
            f"numAnalysisThreads = {params['numAnalysisThreads']}",
        ]
        cfg_path = tmpdir / f"tune_{params['nnMaxBatchSize']}_{params['numSearchThreads']}.cfg"
        cfg_path.write_text("\n".join(cfg_lines) + "\n")

        # 运行基准测试
        t0 = time.time()
        try:
            result = _run_single_benchmark(katago_path, model_path, str(cfg_path),
                                           test_sgf_path, visits)
            dt = time.time() - t0
            if result["success"]:
                vps = visits * result["moves"] / max(dt, 0.1)
                results.append({**params, "duration_s": round(dt, 2),
                               "vps": round(vps, 1), "moves": result["moves"],
                               "success": True})
            else:
                results.append({**params, "success": False})
        except Exception:
            results.append({**params, "success": False})

    # 选最优
    best = max(results, key=lambda r: r.get("vps", 0)) if results else {}

    # 生成推荐配置
    if best:
        recommended = tmpdir / "recommended.cfg"
        cfg_lines = list(base_lines) + [
            f"numSearchThreads = {best['numSearchThreads']}",
            f"nnMaxBatchSize = {best['nnMaxBatchSize']}",
            f"numAnalysisThreads = {best['numAnalysisThreads']}",
        ]
        recommended.write_text("\n".join(cfg_lines) + "\n")

    return {
        "best_config": {
            "numSearchThreads": best.get("numSearchThreads"),
            "nnMaxBatchSize": best.get("nnMaxBatchSize"),
            "numAnalysisThreads": best.get("numAnalysisThreads"),
            "vps": best.get("vps"),
        },
        "results": results,
        "recommended_config_path": str(recommended) if best else "",
        "tmpdir": str(tmpdir),
    }


def _run_single_benchmark(katago_path, model_path, config_path, test_sgf, visits):
    """运行单次基准测试。"""
    from ..analysis.sgf_parser import extract_main_line
    content = Path(test_sgf).read_text(encoding="utf-8", errors="replace")
    moves = extract_main_line(content)[:30]
    if not moves:
        return {"success": False}

    try:
        proc = subprocess.Popen(
            [katago_path, "analysis", "-model", model_path, "-config", config_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        queries = [
            json.dumps({
                "id": f"g_{i}", "moves": moves[:i],
                "maxVisits": visits, "rules": "chinese", "komi": 7.5,
                "boardXSize": 19, "boardYSize": 19, "includePolicy": True,
            })
            for i in range(len(moves))
        ]
        proc.stdin.write("\n".join(queries) + "\n")
        proc.stdin.flush()

        count = 0
        while count < len(moves):
            line = proc.stdout.readline()
            if not line:
                break
            count += 1
        proc.stdin.close()
        proc.terminate()
        proc.wait(3)
        return {"success": count > 0, "moves": count}
    except Exception:
        return {"success": False}


def tune(
    katago_path: str,
    model_path: str,
    test_sgf_path: str,
    config_path: Optional[str] = None,
    target_vps: float = 10.0,
) -> dict:
    """自动调优 — 在 benchmark 结果中选择满足目标 VPS 的最大 visits。"""
    bm = benchmark(katago_path, model_path, test_sgf_path, config_path=config_path)
    results = bm.get("results", [])
    passing = [r for r in results if r.get("success") and r.get("vps", 0) >= target_vps]
    if passing:
        best = max(passing, key=lambda r: r["visits"])
        bm["chosen_visits"] = best["visits"]
    else:
        bm["chosen_visits"] = bm.get("best_visits", 25)
    return bm

"""Tuning — KataGo 基准测试与参数调优。"""
import json
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

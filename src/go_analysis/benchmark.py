"""
Benchmark & Tuning — KataGo 性能基准测试与配置调优。

在不同配置下运行短时分析, 对比速度 (visits/sec), 找到最优参数。

本地运行::

    go-analyzer benchmark --sgf sample.sgf

远程运行::

    go-analyzer benchmark --sgf sample.sgf --platform ssh --host 192.168.9.31 --user xiaoj
"""

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .analyzer import create_adapter
from .analyzer.protocol import AnalysisProtocol
from .sgf_parser import SGF


# ── 基准测试配置网格 ─────────────────────────────

TUNE_GRID = {
    "numAnalysisThreads": [1, 2, 4, 8],
    "numSearchThreads": [4, 8, 16, 24],
    "nnMaxBatchSize": [4, 8, 16],
}

# 推荐配置 (GPU 经验值)
GPU_RECOMMENDATIONS = {
    "GTX 1660": {"numAnalysisThreads": 2, "numSearchThreads": 16, "nnMaxBatchSize": 8},
    "RTX 3060": {"numAnalysisThreads": 4, "numSearchThreads": 24, "nnMaxBatchSize": 16},
    "RTX 3070": {"numAnalysisThreads": 6, "numSearchThreads": 32, "nnMaxBatchSize": 16},
    "RTX 3080": {"numAnalysisThreads": 8, "numSearchThreads": 40, "nnMaxBatchSize": 16},
    "RTX 3090": {"numAnalysisThreads": 10, "numSearchThreads": 48, "nnMaxBatchSize": 16},
    "RTX 4060": {"numAnalysisThreads": 4, "numSearchThreads": 24, "nnMaxBatchSize": 8},
    "RTX 4070": {"numAnalysisThreads": 6, "numSearchThreads": 32, "nnMaxBatchSize": 16},
    "RTX 4080": {"numAnalysisThreads": 8, "numSearchThreads": 40, "nnMaxBatchSize": 16},
    "RTX 4090": {"numAnalysisThreads": 12, "numSearchThreads": 48, "nnMaxBatchSize": 16},
    "A100": {"numAnalysisThreads": 16, "numSearchThreads": 64, "nnMaxBatchSize": 32},
}


@dataclass
class BenchmarkResult:
    """单次基准测试结果"""
    analysis_threads: int
    search_threads: int
    batch_size: int
    visits_per_sec: float
    total_time_s: float
    positions: int


def detect_gpu(platform: str = "auto", host: Optional[str] = None,
               user: Optional[str] = None) -> str:
    """检测 GPU 型号。"""
    if platform == "ssh" and host:
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", f"{user}@{host}",
               "nvidia-smi --query-gpu=name --format=csv,noheader 2>nul || echo unknown"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    else:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip()
        except Exception:
            return "unknown"


def recommend_config(gpu_model: str) -> dict:
    """根据 GPU 推荐配置。"""
    for key, cfg in GPU_RECOMMENDATIONS.items():
        if key.lower() in gpu_model.lower():
            return cfg
    # 未匹配: 保守配置
    return {"numAnalysisThreads": 2, "numSearchThreads": 16, "nnMaxBatchSize": 8}


def run_benchmark(sgf_content: str, kata_path: str, model_path: str,
                  config_path: str, visits: int = 25,
                  analysis_threads: int = 2, search_threads: int = 16,
                  batch_size: int = 8, platform: str = "windows_native",
                  host: Optional[str] = None, port: int = 22,
                  user: Optional[str] = None) -> BenchmarkResult:
    """运行一次基准测试。"""
    # 临时配置
    import tempfile
    cfg_content = (
        f"logDir = analysis_logs\n"
        f"reportAnalysisWinratesAs = BLACK\n"
        f"analysisPVLen = 15\n"
        f"wideRootNoise = 0.04\n"
        f"numAnalysisThreads = {analysis_threads}\n"
        f"numSearchThreads = {search_threads}\n"
        f"nnMaxBatchSize = {batch_size}\n"
    )
    tmp_cfg = tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False)
    tmp_cfg.write(cfg_content)
    tmp_cfg.close()

    try:
        if platform == "ssh" and host:
            # 远程基准: 通过 SSH 运行
            import select
            ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no",
                       "-o", "ConnectTimeout=15",
                       f"{user}@{host}",
                       f'"{kata_path}" analysis -model "{model_path}" -config -']
            proc = subprocess.Popen(ssh_cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
            time.sleep(2)
            # 用临时配置
            proc.stdin.write(cfg_content + "\n")
            proc.stdin.flush()
        else:
            adapter = create_adapter(
                platform=platform,
                kata_path=kata_path,
                model_path=model_path,
                config_path=tmp_cfg.name,
                visits=visits,
                num_analysis_threads=analysis_threads,
                num_search_threads=search_threads,
            )
            adapter.start()

        # 解析 SGF
        tree = SGF.parse(sgf_content)
        all_moves = []
        for node in tree.nodes_in_tree:
            m = node.move
            if m and not m.is_pass:
                all_moves.append([m.player, m.gtp()])

        if not all_moves:
            raise ValueError("No moves in SGF")

        # 发送所有位置查询
        t0 = time.time()
        if platform == "ssh" and host:
            responses = {}
            for idx in range(len(all_moves)):
                history = all_moves[:idx]
                q = AnalysisProtocol.build_query(f"b_{idx}", history, visits)
                proc.stdin.write(q + "\n")
            proc.stdin.flush()
            proc.stdin.close()
            for idx in range(len(all_moves)):
                line = proc.stdout.readline()
                if not line:
                    break
                resp = json.loads(line.strip())
                rid = resp.get("id", "")
                parts = rid.split("_")
                if parts:
                    responses[int(parts[-1])] = resp
            proc.wait(timeout=120)
        else:
            result = adapter.analyze(sgf_content, visits=visits)
            responses_count = len(result.raw_json.get("features_list", []))

        total_time = time.time() - t0

        visits_total = visits * len(all_moves)
        vps = visits_total / total_time if total_time > 0 else 0

        return BenchmarkResult(
            analysis_threads=analysis_threads,
            search_threads=search_threads,
            batch_size=batch_size,
            visits_per_sec=vps,
            total_time_s=total_time,
            positions=len(all_moves),
        )
    finally:
        Path(tmp_cfg.name).unlink(missing_ok=True)
        if platform != "ssh":
            try:
                adapter.shutdown()
            except Exception:
                pass


def tune(sgf_file: str | Path, kata_path: str, model_path: str,
         config_path: str, platform: str = "windows_native",
         host: Optional[str] = None, port: int = 22,
         user: Optional[str] = None) -> dict:
    """
    自动调优: 搜索最佳配置参数。

    Returns
    -------
    dict
        最佳配置 + 基准结果
    """
    sgf_content = Path(sgf_file).read_text(encoding="utf-8", errors="replace")

    # 1. 检测 GPU
    gpu = detect_gpu(platform, host, user)
    print(f"[Tune] GPU: {gpu}")

    # 2. 推荐初始配置
    best_cfg = recommend_config(gpu)
    print(f"[Tune] Recommended: {best_cfg}")

    # 3. 搜索最优
    best_vps = 0
    results = []

    for threads in TUNE_GRID["numAnalysisThreads"]:
        for search_th in TUNE_GRID["numSearchThreads"]:
            for bs in TUNE_GRID["nnMaxBatchSize"]:
                try:
                    r = run_benchmark(
                        sgf_content=sgf_content,
                        kata_path=kata_path,
                        model_path=model_path,
                        config_path=config_path,
                        visits=25,
                        analysis_threads=threads,
                        search_threads=search_th,
                        batch_size=bs,
                        platform=platform,
                        host=host, port=port, user=user,
                    )
                    results.append(r)
                    print(f"  A{threads}S{search_th}B{bs}: {r.visits_per_sec:.0f} vps ({r.total_time_s:.1f}s)")

                    if r.visits_per_sec > best_vps:
                        best_vps = r.visits_per_sec
                        best_cfg = {
                            "numAnalysisThreads": threads,
                            "numSearchThreads": search_th,
                            "nnMaxBatchSize": bs,
                        }
                except Exception as e:
                    print(f"  A{threads}S{search_th}B{bs}: FAILED ({e})")

    return {
        "gpu": gpu,
        "best_config": best_cfg,
        "best_vps": best_vps,
        "all_results": [
            {"analysis_threads": r.analysis_threads,
             "search_threads": r.search_threads,
             "batch_size": r.batch_size,
             "vps": round(r.visits_per_sec, 0),
             "time_s": round(r.total_time_s, 1)}
            for r in results
        ],
    }

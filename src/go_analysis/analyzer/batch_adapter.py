"""
batch_adapter.py — go-analyzer 到 KataGo batch_analysis 的适配层。

替换旧版逐手 analysis 引擎：
  - 旧: SGF → extract_main_line → analysis per-move → 12D feature → NPZ
  - 新: SGF → katago batch_analysis → KAB2 NPZ (10+512+512 + HumanSL)

依赖:
  - katago.exe v1.16.5-trunk 或更新 (https://github.com/ahillzhao-msn/KataGo)
  - Windows OpenCL 后端
"""
import os
import subprocess
import struct
import zlib
from pathlib import Path
from typing import Optional
import numpy as np

KATAGO_RELEASE = "https://github.com/ahillzhao-msn/KataGo/releases/download/v1.16.5-trunk/katago.exe"

# KAB2 format constants
SCALAR_DIM = 10
HEADER_BYTES = 96
RANK_NAMES = [
    "20k","19k","18k","17k","16k","15k","14k","13k","12k","11k","10k","9k",
    "8k","7k","6k","5k","4k","3k","2k","1k","1d","2d","3d","4d","5d","6d","7d","8d","9d",
]


def load_kab2_npz(path: str) -> dict:
    """读取单侧 KAB2 NPZ 文件, 返回结构化 dict."""
    with open(path, "rb") as f:
        raw = f.read()

    hdr = raw[:HEADER_BYTES]
    n, sc, tr, pk = struct.unpack("iiii", hdr[4:20])
    assert sc == SCALAR_DIM, f"Expected scalar_dim={SCALAR_DIM}, got {sc}"

    # PlayerSummary
    s16 = struct.unpack("16f", hdr[32:96])
    summary = {
        "accuracy1": s16[0],
        "accuracy3": s16[1],
        "meanLogPrior": s16[2],
        "meanWinRate": s16[3],
        "meanScoreLead": s16[4],
        "meanComplexity": s16[5],
        "scoreVariance": s16[6],
        "approxScoreDrop": s16[8],
        "meanWinDelta": s16[9],
        "meanScoreDelta": s16[10],
        "humanRankIdx": int(s16[10]) if s16[10] >= 0 else -1,
        "humanLogPrior": s16[11],
    }
    rank_idx = int(s16[10] + 0.5) if s16[10] >= 0 else -1
    summary["humanRank"] = RANK_NAMES[rank_idx] if 0 <= rank_idx < len(RANK_NAMES) else "?"

    # Per-move data
    flags = struct.unpack("i", hdr[28:32])[0]
    payload = raw[HEADER_BYTES:]
    if flags & 1:
        cl = struct.unpack("i", payload[:4])[0]
        payload = zlib.decompress(payload[4:4+cl])

    arr = np.frombuffer(payload, dtype=np.float32).reshape(n, sc + tr + pk)
    return {
        "num_moves": n,
        "trunk_channels": tr,
        "scalars": arr[:, :sc],        # (N, 10)
        "avg_trunk": arr[:, sc:sc+tr], # (N, C)
        "pick": arr[:, sc+tr:],        # (N, C)
        "summary": summary,
    }


def run_batch_analysis(
    sgf_dir: str,
    output_dir: str,
    katago_path: str,
    model_path: str,
    human_model_path: str = "",
    config_path: str = "",
    visits: int = 25,
) -> dict:
    """执行 katago batch_analysis, 收集结果."""
    os.makedirs(output_dir, exist_ok=True)

    cmd = [
        katago_path, "batch_analysis",
        "-model", model_path,
        "-output-dir", output_dir,
        "-sgf-dir", sgf_dir,
        "-visits", str(visits),
    ]
    if human_model_path:
        cmd += ["-human-model", human_model_path]
    if config_path:
        cmd += ["-config", config_path]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0:
        raise RuntimeError(f"batch_analysis failed: {result.stderr}")

    # Parse summary from the first NPZ
    npz_files = sorted(Path(output_dir).glob("*.npz"))
    if not npz_files:
        return {"games": 0, "error": "no output"}

    games = {}
    for npz in npz_files:
        if npz.name.startswith("_"):
            continue
        side = "B" if "_B" in npz.stem else "W"
        game_id = npz.stem.rsplit("_", 1)[0]
        data = load_kab2_npz(str(npz))
        if game_id not in games:
            games[game_id] = {}
        games[game_id][side] = data

    return {
        "games": len(games),
        "game_data": games,
        "output_dir": output_dir,
    }


def download_katago(target_dir: str) -> str:
    """从 GitHub Release 下载 katago.exe."""
    target = Path(target_dir) / "katago.exe"
    if target.exists():
        return str(target)

    import urllib.request
    print(f"Downloading katago.exe from {KATAGO_RELEASE}...")
    urllib.request.urlretrieve(KATAGO_RELEASE, target)
    target.chmod(0o755)
    print(f"  → {target} ({target.stat().st_size // 1024}KB)")
    return str(target)

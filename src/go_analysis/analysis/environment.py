"""environment — 硬件/软件/棋谱环境采集。"""
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from typing import Optional

from ..data.format import HardwareEnv, SoftwareEnv, GameMeta


def collect_hardware() -> HardwareEnv:
    """采集硬件环境信息。"""
    env = HardwareEnv()

    # CPU
    try:
        if platform.system() == "Linux":
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        env.cpu_model = line.split(":", 1)[1].strip()
                        break
        env.cpu_cores = os.cpu_count() or 0
    except Exception:
        pass

    # GPU
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(", ")
            if len(parts) >= 2:
                env.gpu_model = parts[0].strip()
                mem_str = parts[1].strip().lower().replace("mib", "").strip()
                try:
                    env.gpu_memory_mb = int(float(mem_str))
                except ValueError:
                    pass
    except Exception:
        pass

    # RAM
    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        env.ram_gb = round(kb / (1024 * 1024), 1)
                        break
    except Exception:
        pass

    return env


def collect_software() -> SoftwareEnv:
    """采集软件环境信息。"""
    env = SoftwareEnv()
    env.os = f"{platform.system()} {platform.release()}"
    env.python_version = sys.version
    try:
        result = subprocess.run(["nvidia-smi", "--version"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "CUDA" in line:
                    env.cuda_version = line.strip()
                    break
    except Exception:
        pass
    return env


def extract_game_meta_from_sgf(sgf_content: str, game_id: str = "",
                                 group: str = "") -> GameMeta:
    """从 SGF 内容中提取棋谱元数据。"""
    header = sgf_content[:2000]

    meta = GameMeta(game_id=game_id, group=group)

    # 玩家名
    m = re.search(r'PB\[([^\]]*)\]', header)
    if m:
        meta.black_player = m.group(1)
    m = re.search(r'PW\[([^\]]*)\]', header)
    if m:
        meta.white_player = m.group(1)

    # 段位
    m = re.search(r'BR\[([^\]]*)\]', header)
    if m:
        meta.black_rank = int(m.group(1)) if m.group(1).isdigit() else None
    m = re.search(r'WR\[([^\]]*)\]', header)
    if m:
        meta.white_rank = int(m.group(1)) if m.group(1).isdigit() else None

    # 贴目
    m = re.search(r'KM\[([^\]]*)\]', header)
    if m:
        try:
            meta.komi = float(m.group(1))
        except ValueError:
            pass

    # 结果
    m = re.search(r'RE\[([^\]]*)\]', header)
    if m:
        meta.result = m.group(1)

    # 棋盘大小
    m = re.search(r'SZ\[([^\]]*)\]', header)
    if m:
        try:
            meta.board_size = int(m.group(1))
        except ValueError:
            pass

    # 让子
    m = re.search(r'HA\[([^\]]*)\]', header)
    if m:
        try:
            meta.handicap = int(m.group(1))
        except ValueError:
            pass

    return meta

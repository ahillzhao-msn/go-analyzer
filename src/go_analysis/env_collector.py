"""
三大环境向量采集模块。

在 KataGo 批量分析会话开始时调用一次，收集：
  - 硬件环境向量 (CPU/GPU/RAM/VRAM)
  - 软件环境向量 (CUDA/Torch/KataGo 版本 + 配置)
  - 棋谱环境向量 (从 SGF 或分析上下文提取)

使用方式::

    from .env_collector import collect_hardware, collect_software, extract_game_meta_from_sgf

    hw = collect_hardware()
    sw = collect_software(katago_config={"maxVisits": 18, ...})
    game = extract_game_meta_from_sgf(sgf_content, sgf_path="game.sgf")
"""

import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from typing import Optional

from .analysis_format import HardwareEnv, SoftwareEnv, GameMeta


def collect_hardware() -> HardwareEnv:
    """采集当前系统硬件信息。"""
    env = HardwareEnv(timestamp=datetime.now().isoformat())

    # CPU
    try:
        if sys.platform == "linux":
            with open("/proc/cpuinfo") as f:
                cpuinfo = f.read()
            # Model name
            m = re.search(r"model name\s+:\s+(.+)", cpuinfo)
            if m:
                env.cpu_model = m.group(1).strip()
            env.cpu_cores = os.cpu_count() or 0
        elif sys.platform == "win32":
            result = subprocess.run(
                ["wmic", "cpu", "get", "name", "/format:csv"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split("\n")
            if len(lines) > 1:
                env.cpu_model = lines[-1].strip().split(",")[-1].strip()
            env.cpu_cores = os.cpu_count() or 0
    except Exception:
        env.cpu_model = platform.processor() or "unknown"
        env.cpu_cores = os.cpu_count() or 0

    env.cpu_threads = os.cpu_count() or 0

    # GPU (nvidia-smi)
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,count",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            env.gpu_count = len(lines)
            if lines:
                parts = lines[0].split(", ")
                env.gpu_model = parts[0].strip()
                if len(parts) > 1:
                    try:
                        env.gpu_vram_gb = float(parts[1].strip()) / 1024
                    except ValueError:
                        pass
    except Exception:
        pass

    # RAM
    try:
        if sys.platform == "linux":
            with open("/proc/meminfo") as f:
                meminfo = f.read()
            m = re.search(r"MemTotal:\s+(\d+) kB", meminfo)
            if m:
                env.ram_gb = float(m.group(1)) / 1024 / 1024
    except Exception:
        pass

    return env


def collect_software(
    katago_path: str = "katago",
    katago_model: str = "",
    max_visits: int = 50,
    num_threads: int = 4,
) -> SoftwareEnv:
    """采集当前系统软件环境信息。

    Parameters
    ----------
    katago_path : str
        KataGo 可执行文件路径，用于探测版本号。
    katago_model : str
        使用的 KataGo 模型权重文件名。
    max_visits : int
        分析时使用的 maxVisits 配置。
    num_threads : int
        分析线程数。
    """
    env = SoftwareEnv(
        katago_model=katago_model or os.path.basename(katago_model) if katago_model else "",
        katago_max_visits=max_visits,
        katago_num_threads=num_threads,
        os_info=f"{platform.system()} {platform.release()}",
    )

    # Python
    env.python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # CUDA
    try:
        result = subprocess.run(
            ["nvcc", "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            m = re.search(r"release (\S+)", result.stdout)
            if m:
                env.cuda_version = m.group(1)
    except Exception:
        pass

    # CUDA from nvidia-smi fallback
    if not env.cuda_version:
        try:
            result = subprocess.run(
                ["nvidia-smi"], capture_output=True, text=True, timeout=5
            )
            m = re.search(r"CUDA Version:\s*(\S+)", result.stdout)
            if m:
                env.cuda_version = m.group(1)
        except Exception:
            pass

    # Torch
    try:
        import torch
        env.torch_version = torch.__version__
    except ImportError:
        env.torch_version = "not_installed"

    # KataGo version
    try:
        result = subprocess.run(
            [katago_path, "version"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            env.katago_version = result.stdout.strip()
    except Exception:
        pass

    return env


RANK_PATTERN = re.compile(r"(\d+\s*[dkp段])", re.IGNORECASE)


def _normalize_rank(rank_str: str) -> str:
    """统一段位格式，如 '5段' → '5d', '3K' → '3k'."""
    m = RANK_PATTERN.search(rank_str)
    if not m:
        return ""
    raw = m.group(1).strip().lower()
    # 中文段位 → d
    if raw.endswith("段"):
        return raw[0] + "d"
    return raw


def extract_game_meta_from_sgf(
    sgf_content: str,
    sgf_path: str = "",
    game_id: str = "",
) -> GameMeta:
    """从 SGF 内容提取棋谱环境向量。

    解析 SGF 头部信息，提取:
    PB/PW — 选手名, BR/WR — 段位, RE — 结果,
    KM — 贴目, HA — 让子, SZ — 棋盘大小,
    RU — 规则, DT — 日期, EV — 赛事名,
    TM — 时间设定, 以及总步数 (;B/;W 计数).
    """
    meta = GameMeta(sgf_path=sgf_path, game_id=game_id)

    def _get(prop: str) -> str:
        m = re.search(rf"{re.escape(prop)}\[([^\]]*)\]", sgf_content)
        return m.group(1).strip() if m else ""

    meta.player_black = _get("PB")
    meta.player_white = _get("PW")
    meta.rank_black = _normalize_rank(_get("BR"))
    meta.rank_white = _normalize_rank(_get("WR"))
    meta.result = _get("RE")

    # Komi
    km = _get("KM")
    try:
        meta.komi = float(km) if km else 7.5
    except ValueError:
        meta.komi = 7.5

    # Handicap
    ha = _get("HA")
    try:
        meta.handicap = int(ha) if ha else 0
    except ValueError:
        meta.handicap = 0

    # Board size
    sz = _get("SZ")
    try:
        meta.board_size = int(sz) if sz else 19
    except ValueError:
        meta.board_size = 19

    meta.rules = _get("RU") or "chinese"
    meta.game_date = _get("DT")
    meta.event_name = _get("EV")
    meta.time_settings = _get("TM")

    # Total moves: count ;B or ;W tokens
    meta.total_moves = len(re.findall(r";[BW]", sgf_content, re.IGNORECASE))

    # 归一化所有字段
    meta.normalize()

    return meta


def extract_game_meta_from_sgf_file(sgf_path: str, game_id: str = "") -> GameMeta:
    """从 SGF 文件提取棋谱环境向量。"""
    try:
        with open(sgf_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (FileNotFoundError, UnicodeDecodeError):
        # Try binary fallback
        try:
            with open(sgf_path, "rb") as f:
                content = f.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return GameMeta(sgf_path=sgf_path, game_id=game_id or sgf_path)

    game_id = game_id or os.path.basename(sgf_path)
    return extract_game_meta_from_sgf(content, sgf_path=sgf_path, game_id=game_id)

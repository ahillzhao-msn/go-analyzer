"""
KataGo 自动发现 — 扫描本地 & WSL & Windows 环境。

用法::

    from go_analysis.discovery import discover_katago

    hosts = discover_katago()
    for h in hosts:
        print(h["name"], h["platform"], h["kata_path"])
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


# ── 扫描路径配置 ─────────────────────────────────────

# WSL Linux 常见路径
LINUX_PATHS = [
    "/usr/local/bin/katago",
    "/usr/bin/katago",
    "/opt/katago/katago",
    "./katago_install/katago",
]

# WSL → Windows interop 路径 (KataGo Windows exe)
WINDOWS_INTEROP_PATHS = [
    "/mnt/c/Users/*/go/katago.exe",
    "/mnt/c/Users/*/katago/katago.exe",
    "/mnt/c/Users/*/sgoinfre/katago.exe",
]

# Windows 常见 KataGo 安装目录 (相对项目)
PROJECT_KATAGO_PATHS = [
    "kata-go/windows/katago-v1.16.5-opencl-windows-x64.exe",
    "kata-go/windows/v1.16.4/katago.exe",
    "kata-go/linux/katago",
]

# 模型路径 (相对于 kata 目录)
MODEL_RELATIVE_PATHS = [
    "../models/kata1-b18c384nbt-s6582191360-d3422816034.bin.gz",
    "../models/b18c384nbt-humanv0.bin.gz",
    "models/kata1-b18c384nbt-s6582191360-d3422816034.bin.gz",
]

# OpenCL tuning 目录 (Windows)
OPENCL_TUNING_DIRS = [
    "KataGoData/opencltuning/",
    "../KataGoData/opencltuning/",
]


def discover_katago(project_root: str | Path = ".") -> list[dict]:
    """
    自动发现环境中所有可用的 KataGo 安装。

    Returns
    -------
    list[dict]
        每个 dict 包含: name, platform, kata_path, model_path, config_path
    """
    project_root = Path(project_root).resolve()
    hosts = []

    # 1. 扫描项目目录
    for rel_path in PROJECT_KATAGO_PATHS:
        full_path = project_root / rel_path
        if full_path.exists():
            host = _make_host(full_path, project_root)
            if host:
                hosts.append(host)

    # 2. 扫描 Linux 系统路径
    for path in LINUX_PATHS:
        p = Path(path).expanduser()
        if p.exists():
            host = _make_host(p, project_root)
            if host:
                hosts.append(host)

    # 3. 扫描 Windows interop 路径
    for pattern in WINDOWS_INTEROP_PATHS:
        from glob import glob
        for match in glob(pattern):
            p = Path(match)
            if p.exists():
                host = _make_host(p, project_root, platform="windows_native")
                if host:
                    hosts.append(host)

    # 去重: 相同路径只保留一个
    seen = set()
    unique = []
    for h in hosts:
        key = h["kata_path"]
        if key not in seen:
            seen.add(key)
            unique.append(h)

    return unique


def _make_host(kata_path: Path, project_root: Path,
               platform: Optional[str] = None) -> Optional[dict]:
    """从 kata 路径推断主机配置"""
    if not kata_path.exists():
        return None

    kata_str = str(kata_path)
    is_exe = kata_str.endswith(".exe")

    # 自动判断平台
    if platform:
        pass  # 使用指定平台
    elif is_exe:
        platform = "windows_native"
    else:
        platform = "auto"  # Linux 原生

    # 寻找模型
    model_path = _find_model(kata_path, project_root)

    # 寻找配置
    config_path = None
    if is_exe:
        # Windows 配置在 exe 同目录
        cfg_candidates = [
            kata_path.parent / "analysis_config.cfg",
            project_root / "kata-go/windows/analysis_config.cfg",
        ]
        for c in cfg_candidates:
            if c.exists():
                config_path = str(c)
                break

    # 生成主机名
    name = f"katago-{platform}-{kata_path.parent.name}"

    host = {
        "name": name,
        "platform": platform,
        "host": "localhost",
        "kata_path": kata_str,
        "model_path": str(model_path) if model_path else None,
        "config_path": config_path,
        "max_concurrent": 3 if not is_exe else 1,
        "capabilities": _detect_capabilities(kata_str),
    }
    return host


def _find_model(kata_path: Path, project_root: Path) -> Optional[Path]:
    """在 kata 附近寻找模型文件"""
    for rel in MODEL_RELATIVE_PATHS:
        candidates = [
            kata_path.parent / rel,
            project_root / rel,
            project_root / "kata-go/models" / Path(rel).name,
        ]
        for c in candidates:
            if c.exists():
                return c
    return None


def _detect_capabilities(kata_path: str) -> list[str]:
    """检测 KataGo 能力 (GPU/CPU/OpenCL)"""
    caps = []
    if kata_path.endswith(".exe"):
        caps.append("opencl")
        caps.append("windows")
    else:
        caps.append("cpu")
        # 检查 GPU (通过 nvidia-smi)
        try:
            subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
            caps.append("cuda")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return caps


def register_discovered_hosts(config, hosts: list[dict]):
    """将发现的主机写入配置"""
    existing = {h.get("name") for h in config.get("hosts", [])}
    for h in hosts:
        if h["name"] not in existing:
            config.setdefault("hosts", []).append(h)

"""Discovery — KataGo 自动发现与环境检测。"""

import os
import platform
import subprocess
from pathlib import Path
from typing import Optional


def discover_katago() -> list[dict]:
    """自动扫描可用 KataGo 安装，返回按优先级排序的列表。

    Returns:
        [{"path": str, "model": str, "config": str, "type": str}, ...]
    """
    results = []

    # 1. 项目自带 KataGo (优先)
    proj_root = Path(__file__).resolve().parent.parent.parent.parent
    kata_go_base = proj_root / "kata-go"

    # WSL → Windows KataGo (通过 WSL interop)
    if kata_go_base.exists():
        for katago in sorted(kata_go_base.rglob("katago*")):
            if katago.is_file() and str(katago) not in [p["path"] for p in results]:
                model = _find_model(katago.parent)
                config = katago.parent / "analysis_config.cfg"
                if not config.exists():
                    config = kata_go_base / "windows" / "analysis_config.cfg"
                results.append({
                    "path": str(katago),
                    "model": str(model or ""),
                    "config": str(config) if config.exists() else "",
                    "type": "project",
                })

    # 3. 环境变量 KATAGO_PATH
    env_path = os.environ.get("KATAGO_PATH", "")
    if env_path and env_path not in [p["path"] for p in results]:
        model = _find_model(Path(env_path).parent)
        results.append({
            "path": env_path,
            "model": str(model or ""),
            "config": "",
            "type": "env",
        })

    # 4. PATH 中的 katago
    try:
        which = subprocess.run(["which", "katago"], capture_output=True, text=True, timeout=5)
        if which.returncode == 0:
            katago_path = which.stdout.strip()
            if katago_path not in [p["path"] for p in results]:
                model = _find_model(Path(katago_path).parent)
                results.append({
                    "path": katago_path,
                    "model": str(model or ""),
                    "config": "",
                    "type": "path",
                })
    except Exception:
        pass

    return results


def _find_model(base_dir: Path) -> Optional[Path]:
    """在目录及其子目录中查找 KataGo 模型文件。"""
    patterns = ["*.bin.gz", "*.pt", "*.onnx"]
    for pattern in patterns:
        matches = sorted(base_dir.rglob(pattern))
        if matches:
            return matches[0]
    return None


def register_discovered_hosts(discovered: list[dict]) -> list[str]:
    """注册发现的主机，返回可用的主机标识列表。"""
    available = []
    for entry in discovered:
        path = entry["path"]
        if Path(path).exists():
            available.append(entry["type"])
    return available

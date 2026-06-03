"""
Worker Deploy — SSH远程自动部署 KataGo。

流程::

    1. SSH 连远程主机
    2. 检查是否已有 KataGo
    3. 没有则创建 ~/go-analyzer-worker/
    4. 复制 kata-go 二进制、模型、配置
    5. 初始化 OpenCL tuning / 验证
    6. 更新 config.yaml 注册表

使用::

    from go_analysis.worker_deploy import deploy_ssh

    result = deploy_ssh("192.168.9.31", user="ahill")
    print(result["kata_path"])   # 远程 KataGo 路径
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional


# ── 远程路径 ─────────────────────────────────────────

REMOTE_DIR = "go-analyzer-worker"
REMOTE_KATA_DIR = f"{REMOTE_DIR}/katago"
REMOTE_MODELS_DIR = f"{REMOTE_DIR}/models"


def deploy_ssh(host: str, user: str = "root", port: int = 22,
               identity_file: Optional[str] = None,
               kata_binary: Optional[str] = None,
               model_file: Optional[str] = None,
               config_file: Optional[str] = None,
               project_root: str = ".") -> dict:
    """
    在远程主机上部署 KataGo 分析环境.

    Parameters
    ----------
    host : str
        远程主机 IP.
    user : str
        SSH 用户名.
    port : int
        SSH 端口.
    identity_file : str or None
        SSH 私钥.
    kata_binary : str or None
        本机 KataGo 可执行文件路径. None = 项目默认.
    model_file : str or None
        本机模型文件路径. None = 项目默认.
    config_file : str or None
        本机配置路径. None = 项目默认.
    project_root : str
        项目根目录.

    Returns
    -------
    dict
        {kata_path, model_path, config_path, name, success}
    """
    project_root = Path(project_root).resolve()

    # ── 确定本地文件 ────────────────────────────────
    if not kata_binary:
        kata_binary = _find_local_kata(project_root)
    if not model_file:
        model_file = _find_local_model(project_root)
    if not config_file:
        config_file = _find_local_config(project_root)

    base_cmd = ["ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30",
                "-p", str(port)]
    if identity_file and os.path.exists(identity_file):
        base_cmd += ["-i", identity_file]
    base_cmd += [f"{user}@{host}"]

    def _run(remote_cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            base_cmd + [remote_cmd],
            capture_output=True, text=True, timeout=timeout,
        )

    print(f"[Deploy] Connecting to {user}@{host}:{port}...")

    # ── Step 0: 采集远程环境信息 ──────────────────
    remote_env = _collect_remote_env(base_cmd, _run)
    print(f"[Deploy] Remote environment:")
    for k, v in sorted(remote_env.items()):
        print(f"    {k}: {v}")

    # ── Step 1: 自动发现远程 KataGo ────────────────
    # 检查多个常见路径
    discover_cmd = (
        'for p in ~/.katago/katago ~/go-analyzer-worker/katago/katago '
        '/usr/local/bin/katago /usr/bin/katago /opt/katago/katago; '
        'do [ -x "$p" ] && echo "FOUND:$p" && break; done; '
        'which katago 2>/dev/null && echo "FOUND:$(which katago)" || true'
    )
    result = _run(discover_cmd)
    existing = ""
    for line in result.stdout.strip().split("\n"):
        if line.startswith("FOUND:"):
            existing = line.split(":", 1)[1].strip()
            break

    if existing:
        print(f"[Deploy] Found KataGo at: {existing}")
        # 检查模型
        model_cmd = (
            'for p in ~/.katago/*.bin.gz ~/go-analyzer-worker/models/*.bin.gz '
            '/opt/katago/models/*.bin.gz; '
            'do [ -f "$p" ] && echo "FOUND:$p" && break; done'
        )
        model_result = _run(model_cmd)
        model_path = ""
        for line in model_result.stdout.strip().split("\n"):
            if line.startswith("FOUND:"):
                model_path = line.split(":", 1)[1].strip()
                break

        if model_path:
            print(f"[Deploy] Found model at: {model_path}")
            return {
                "name": f"{host}-ssh",
                "kata_path": existing,
                "model_path": model_path,
                "config_path": "",
                "success": True,
                "host": host, "port": port, "user": user,
            }
        else:
            print(f"[Deploy] KataGo found but no model. Will deploy model only.")
            # 只复制模型到 kata 同目录
            kata_dir = Path(existing).parent
            remote_model = f"{kata_dir}/kata1-b18c384nbt-s6582191360-d3422816034.bin.gz"
            if model_file and os.path.exists(model_file):
                print(f"[Deploy] Copying model to {kata_dir}...")
                scp_cmd = ["scp", "-o", "StrictHostKeyChecking=no",
                           "-P", str(port), model_file,
                           f"{user}@{host}:{remote_model}"]
                if identity_file and os.path.exists(identity_file):
                    scp_cmd[1:1] = ["-i", identity_file]
                subprocess.run(scp_cmd, capture_output=True, timeout=300)
                print(f"[Deploy] Model deployed.")
            return {
                "name": f"{host}-ssh",
                "kata_path": existing,
                "model_path": remote_model,
                "config_path": "",
                "success": True,
                "host": host, "port": port, "user": user,
            }

    # ── Step 2: 创建目录结构 ──────────────────────
    print(f"[Deploy] Creating ~/{REMOTE_DIR}/ structure...")
    _run(f"mkdir -p ~/{REMOTE_KATA_DIR} ~/{REMOTE_MODELS_DIR} ~/{REMOTE_DIR}/config")

    # ── Step 3: 检查远程平台 ──────────────────────
    plat_result = _run("uname -s 2>/dev/null || echo Windows")
    plat = plat_result.stdout.strip().lower()
    is_windows = "windows" in plat or "nt" in plat or "microsoft" in plat
    remote_arch = "windows" if is_windows else "linux"
    print(f"[Deploy] Remote platform: {remote_arch}")

    # ── Step 4: 复制二进制 ────────────────────────
    # 根据远程平台选择正确的二进制
    remote_kata_path = f"~/{REMOTE_KATA_DIR}/katago" + (".exe" if is_windows else "")

    if kata_binary and os.path.exists(kata_binary):
        print(f"[Deploy] Copying KataGo binary ({kata_binary})...")
        # 检查远程架构是否匹配
        local_name = Path(kata_binary).name
        if is_windows and "linux" in local_name:
            print(f"[Deploy] WARNING: Local binary is Linux, remote may need Windows exe")
        if not is_windows and ".exe" in local_name:
            print(f"[Deploy] WARNING: Local binary is Windows exe, remote may need Linux")

        # 使用 scp 复制
        scp_cmd = ["scp",
                    "-o", "StrictHostKeyChecking=no",
                    "-P", str(port),
                    kata_binary,
                    f"{user}@{host}:{remote_kata_path}"]
        if identity_file and os.path.exists(identity_file):
            scp_cmd[1:1] = ["-i", identity_file]
        subprocess.run(scp_cmd, capture_output=True, timeout=120)
        _run(f"chmod +x {remote_kata_path}")
        print(f"[Deploy] Copied to: {remote_kata_path}")

    # ── Step 5: 复制模型 ──────────────────────────
    remote_model_path = f"~/{REMOTE_MODELS_DIR}/{Path(model_file).name}"
    if model_file and os.path.exists(model_file):
        print(f"[Deploy] Copying model ({Path(model_file).name})...")
        scp_cmd = ["scp",
                    "-o", "StrictHostKeyChecking=no",
                    "-P", str(port),
                    model_file,
                    f"{user}@{host}:{remote_model_path}"]
        if identity_file and os.path.exists(identity_file):
            scp_cmd[1:1] = ["-i", identity_file]
        subprocess.run(scp_cmd, capture_output=True, timeout=300)
        print(f"[Deploy] Model copied")

    # ── Step 6: 复制配置 ──────────────────────────
    remote_config_path = f"~/{REMOTE_DIR}/config/analysis_config.cfg"
    if config_file and os.path.exists(config_file):
        print(f"[Deploy] Copying config...")
        scp_cmd = ["scp",
                    "-o", "StrictHostKeyChecking=no",
                    "-P", str(port),
                    config_file,
                    f"{user}@{host}:{remote_config_path}"]
        if identity_file and os.path.exists(identity_file):
            scp_cmd[1:1] = ["-i", identity_file]
        subprocess.run(scp_cmd, capture_output=True, timeout=30)

    # ── Step 7: 验证 ──────────────────────────────
    print(f"[Deploy] Verifying installation...")
    verify = _run(f"{remote_kata_path} version 2>&1 || echo FAILED")
    if "FAILED" in verify.stdout:
        print(f"[Deploy] WARNING: Version check failed: {verify.stderr[:200]}")
        success = False
    else:
        print(f"[Deploy] KataGo version: {verify.stdout.strip()[:60]}")
        success = True

    hostname = f"{host}-ssh"
    print(f"[Deploy] Done! Host: {hostname}")
    print(f"  kata_path: {remote_kata_path}")
    print(f"  model_path: {remote_model_path}")
    print(f"  config_path: {remote_config_path}")

    return {
        "name": hostname,
        "kata_path": remote_kata_path,
        "model_path": remote_model_path,
        "config_path": remote_config_path,
        "success": success,
        "host": host,
        "port": port,
        "user": user,
    }


# ── 本地查找辅助 ──────────────────────────────────

def _find_local_kata(project_root: Path) -> Optional[str]:
    """找本机 KataGo 二进制"""
    candidates = [
        project_root / "kata-go/windows/katago-v1.16.5-opencl-windows-x64.exe",
        project_root / "kata-go/windows/v1.16.4/katago.exe",
        project_root / "kata-go/linux/katago",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _find_local_model(project_root: Path) -> Optional[str]:
    """找本机模型"""
    models_dir = project_root / "kata-go/models"
    if models_dir.exists():
        models = sorted(models_dir.glob("kata1-*.bin.gz"))
        if models:
            return str(models[-1])
    return None


def _find_local_config(project_root: Path) -> Optional[str]:
    """找本机配置"""
    candidates = [
        project_root / "kata-go/windows/analysis_config.cfg",
        project_root / "kata-go/windows/v1.16.4/analysis_config.cfg",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


# ── 远程环境采集 ──────────────────────────────────

def _collect_remote_env(base_cmd: list, _run) -> dict:
    """通过 SSH 采集远程主机的硬件/软件环境。

    Returns
    -------
    dict
        os, arch, cpu_model, cpu_cores, ram_gb,
        gpu_model, gpu_vram, cuda_version,
        python_version, has_cuda, has_opencl, has_docker
    """
    env = {}

    # OS / 架构
    r = _run("uname -s 2>/dev/null || echo Windows")
    env["os"] = r.stdout.strip()
    r = _run("uname -m 2>/dev/null || echo unknown")
    env["arch"] = r.stdout.strip()

    # CPU
    r = _run('grep -m1 "model name" /proc/cpuinfo 2>/dev/null || '
             'wmic cpu get name /format:csv 2>/dev/null | tail -1 || '
             'echo "CPU:unknown"')
    env["cpu_model"] = r.stdout.strip().replace("model name : ", "")
    r = _run("nproc 2>/dev/null || echo 0")
    env["cpu_cores"] = int(r.stdout.strip() or 0)

    # RAM
    r = _run("free -g 2>/dev/null | awk '/^Mem:/{print $2}' || "
             "wmic memorychip get capacity /format:csv 2>/dev/null | tail -1 || "
             'echo "0"')
    ram = r.stdout.strip()
    try:
        env["ram_gb"] = int(ram) if ram else 0
    except ValueError:
        env["ram_gb"] = 0

    # GPU (NVIDIA)
    r = _run("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || "
             'echo "no_nvidia"')
    gpu_info = r.stdout.strip()
    if gpu_info and "no_nvidia" not in gpu_info:
        env["gpu_model"] = gpu_info
        env["has_cuda"] = True
        # CUDA version
        r2 = _run("nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null || echo ''")
        env["cuda_version"] = r2.stdout.strip()
    else:
        env["gpu_model"] = ""
        env["has_cuda"] = False
        env["cuda_version"] = ""

    # OpenCL
    r = _run("which clinfo 2>/dev/null && clinfo 2>/dev/null | grep -i 'platform name' || "
             "echo 'no_opencl'")
    env["has_opencl"] = "no_opencl" not in r.stdout.strip().lower()

    # Python
    r = _run("python3 --version 2>/dev/null || python --version 2>/dev/null || echo 'no_python'")
    env["python_version"] = r.stdout.strip().replace("Python ", "")

    return env


# ── KataGo 版本选择 ──────────────────────────────

def _select_kata_version(remote_env: dict, kata_binary: str) -> str:
    """根据远程环境选择 KataGo 二进制。

    Returns
    -------
    str
        要部署的 KataGo 二进制路径 (本地).
    """
    is_linux = "linux" in remote_env.get("os", "").lower()
    is_windows = "microsoft" in remote_env.get("os", "").lower() or "windows" in remote_env.get("os", "").lower()

    # Windows → 只能用 OpenCL exe
    if is_windows:
        if "opencl" in (kata_binary or "").lower():
            return kata_binary
        # fallback
        return os.path.expanduser("~/kata-go/windows/katago-v1.16.5-opencl-windows-x64.exe")

    # Linux
    if is_linux:
        if remote_env.get("has_cuda") and "cuda" in (kata_binary or "").lower():
            return kata_binary
        elif remote_env.get("has_opencl") and "opencl" in (kata_binary or "").lower():
            return kata_binary
        # CPU Eigen — 通用
        if "eigen" in (kata_binary or "").lower():
            return kata_binary

    # 默认: 用本机找到的
    return kata_binary

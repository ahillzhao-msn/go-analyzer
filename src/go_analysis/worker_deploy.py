"""
Worker Deployer — SSH 远程 KataGo 部署与工作区管理。

这是 go-analyzer 分布式分析的基础服务模块。
负责: 远程环境发现、自动部署、工作区管理、版本选择。

用法::

    deployer = WorkerDeployer()
    result = deployer.deploy(host="192.168.9.31", user="ahill")
    deployer.save_to_config(result, "config.yaml")
"""

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("go_analysis.deploy")


# ── 常量 ─────────────────────────────────────────────

REMOTE_DIR = "go-analyzer-worker"
DISCOVERY_PATHS = [
    "~/.katago/katago",
    f"~/{REMOTE_DIR}/katago/katago",
    "/usr/local/bin/katago",
    "/usr/bin/katago",
    "/opt/katago/katago",
]
MODEL_GLOB = "kata1-b18c384nbt-s6582191360-d3422816034.bin.gz"


# ── 数据结构 ─────────────────────────────────────────

@dataclass
class RemoteEnv:
    """远程主机环境信息"""
    os_type: str = ""          # linux / windows
    arch: str = ""
    cpu_model: str = ""
    cpu_cores: int = 0
    ram_gb: float = 0.0
    gpu_model: str = ""
    cuda_version: str = ""
    has_cuda: bool = False
    has_opencl: bool = False
    python_version: str = ""


@dataclass
class DeployResult:
    """部署结果"""
    host: str
    port: int
    user: str
    kata_path: str = ""
    model_path: str = ""
    config_path: str = ""
    workspace: str = f"~/{REMOTE_DIR}"
    success: bool = False
    env: Optional[RemoteEnv] = None
    error: str = ""


# ── SSH 会话包装 ─────────────────────────────────────

class SshSession:
    """SSH 会话 — 封装命令执行和文件传输。"""

    def __init__(self, host: str, user: str, port: int = 22,
                 identity_file: Optional[str] = None):
        self.host = host
        self.user = user
        self.port = port
        self.identity = identity_file

    def _scp(self, local_path: str, remote_path: str,
             timeout: int = 120) -> bool:
        """SCP 传输文件到远程。"""
        cmd = ["scp", "-o", "StrictHostKeyChecking=no",
               "-P", str(self.port)]
        if self.identity and os.path.exists(self.identity):
            cmd += ["-i", self.identity]
        cmd += [local_path, f"{self.user}@{self.host}:{remote_path}"]
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout, check=True)
            return True
        except subprocess.TimeoutExpired:
            logger.warning("SCP timeout (%ss) for %s → %s:%s",
                           timeout, local_path, self.host, remote_path)
            return False
        except subprocess.CalledProcessError as e:
            logger.warning("SCP failed: %s", e.stderr.decode()[:200])
            return False

    def run(self, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
        """在远程上执行命令。"""
        ssh_cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            "-p", str(self.port),
        ]
        if self.identity and os.path.exists(self.identity):
            ssh_cmd += ["-i", self.identity]
        ssh_cmd += [f"{self.user}@{self.host}", cmd]

        def _run():
            return subprocess.run(
                ssh_cmd, capture_output=True, text=True, timeout=timeout,
            )

        return _run()

    def run_retry(self, cmd: str, timeout: int = 30,
                  retries: int = 2) -> subprocess.CompletedProcess:
        """带重试的命令执行。"""
        last_err = None
        for attempt in range(1, retries + 1):
            if attempt > 1:
                time.sleep(2)
            try:
                result = self.run(cmd, timeout)
                if result.returncode == 0:
                    return result
                last_err = result
            except subprocess.TimeoutExpired:
                last_err = None
                logger.warning("SSH timeout (attempt %d/%d)", attempt, retries)
            except Exception as e:
                last_err = None
                logger.warning("SSH error (attempt %d/%d): %s", attempt, retries, e)
        raise RuntimeError(
            f"SSH to {self.user}@{self.host}:{self.port} failed after {retries} retries"
        )

    def test_connection(self, timeout: int = 15) -> bool:
        """测试 SSH 连接是否可达。"""
        try:
            self.run("echo ok", timeout=timeout)
            return True
        except Exception:
            return False


# ── 主部署器 ─────────────────────────────────────────

class WorkerDeployer:
    """远程 KataGo 部署器。

    职责:
    - 远程环境采集 (CPU/GPU/OS/CUDA)
    - KataGo 自动发现
    - 完整部署或仅工作区更新
    - KataGo 版本自动选择
    """

    def __init__(self, project_root: str | Path = "."):
        self._root = Path(project_root).resolve()
        self._kata_binary = self._find_kata_binary()
        self._model_file = self._find_model()
        self._config_file = self._find_config()

    # ── 主入口 ────────────────────────────────────

    def deploy(self, host: str, user: str = "root", port: int = 22,
               identity_file: Optional[str] = None) -> DeployResult:
        """
        在远程主机上部署 KataGo 分析环境。

        Parameters
        ----------
        host : str
        user : str
        port : int
        identity_file : str or None
            SSH 私钥路径。

        Returns
        -------
        DeployResult
        """
        result = DeployResult(host=host, port=port, user=user)

        # ── 连接 ──
        ssh = SshSession(host, user, port, identity_file)
        if not ssh.test_connection():
            result.error = f"SSH unreachable: {user}@{host}:{port}"
            logger.error(result.error)
            return result

        # ── 环境采集 ──
        try:
            result.env = self._collect_env(ssh)
            logger.info("Remote: %s, %s, %d cores, %s GPU",
                        result.env.os_type, result.env.cpu_model,
                        result.env.cpu_cores, result.env.gpu_model or "(none)")
        except Exception as e:
            result.error = f"Env collection failed: {e}"
            logger.error(result.error)
            return result

        # ── 发现 KataGo ──
        try:
            found = self._discover_katago(ssh)
        except Exception as e:
            result.error = f"Discovery failed: {e}"
            logger.error(result.error)
            return result

        if found:
            discovered_path, discovered_model = found
            logger.info("Found KataGo: %s, model: %s", discovered_path, discovered_model)
            self._setup_workspace(ssh)
            result.kata_path = discovered_path
            result.model_path = discovered_model
            result.success = True
            return result

        # ── 完整部署 ──
        try:
            self._deploy_full(ssh, result)
        except Exception as e:
            result.error = f"Deploy failed: {e}"
            logger.error(result.error)
            return result

        result.success = True
        return result

    # ── 远程环境采集 ──────────────────────────────

    def _collect_env(self, ssh: SshSession) -> RemoteEnv:
        env = RemoteEnv()

        r = ssh.run("uname -s")
        env.os_type = r.stdout.strip().lower()
        r = ssh.run("uname -m")
        env.arch = r.stdout.strip()

        r = ssh.run(r"""grep -m1 "model name" /proc/cpuinfo 2>/dev/null \
                     | sed 's/.*: //' || echo unknown""", timeout=10)
        env.cpu_model = r.stdout.strip()
        r = ssh.run("nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 0")
        env.cpu_cores = int(r.stdout.strip() or 0)

        r = ssh.run("free -g 2>/dev/null | awk '/^Mem:/{print $2}' || echo 0")
        try:
            env.ram_gb = float(r.stdout.strip())
        except ValueError:
            env.ram_gb = 0.0

        r = ssh.run("""nvidia-smi --query-gpu=name,memory.total \
                    --format=csv,noheader 2>/dev/null || echo no_nvidia""")
        gpu_line = r.stdout.strip()
        if "no_nvidia" not in gpu_line:
            env.gpu_model = gpu_line
            env.has_cuda = True
            r2 = ssh.run("""nvidia-smi --query-gpu=driver_version \
                         --format=csv,noheader 2>/dev/null || echo ''""")
            env.cuda_version = r2.stdout.strip()

        r = ssh.run("""which clinfo 2>/dev/null || echo no_clinfo""")
        env.has_opencl = "no_clinfo" not in r.stdout.strip()

        r = ssh.run("python3 --version 2>/dev/null | sed 's/Python //' || echo ''")
        env.python_version = r.stdout.strip()

        return env

    # ── KataGo 发现 ───────────────────────────────

    def _discover_katago(self, ssh: SshSession) -> Optional[tuple[str, str]]:
        """发现远程已有的 KataGo 和模型。"""
        # 发现二进制
        path_cmd = "; ".join(
            f'[ -x "{p}" ] && echo "K:{p}" && exit 0'
            for p in DISCOVERY_PATHS
        )
        path_cmd += "; which katago 2>/dev/null && echo \"K:$(which katago)\" || true"

        r = ssh.run(path_cmd)
        kata_path = ""
        for line in r.stdout.strip().split("\n"):
            if line.startswith("K:"):
                kata_path = line[2:].strip()
                break

        if not kata_path:
            return None

        # 发现模型
        kata_dir = Path(kata_path).parent
        model_paths = [
            f"~/.katago/{MODEL_GLOB}",
            f"~/{REMOTE_DIR}/models/{MODEL_GLOB}",
            f"{kata_dir}/{MODEL_GLOB}",
            f"{kata_dir}/../models/{MODEL_GLOB}",
        ]
        model_cmd = "; ".join(
            f'[ -f "{p}" ] && echo "M:{p}" && exit 0'
            for p in model_paths
        )
        model_cmd += "; echo M:"
        r = ssh.run(model_cmd)
        model_path = ""
        for line in r.stdout.strip().split("\n"):
            if line.startswith("M:"):
                v = line[2:].strip()
                if v:
                    model_path = v
                break

        return (kata_path, model_path)

    # ── 工作区设置 (已有 KataGo) ──────────────────

    def _setup_workspace(self, ssh: SshSession):
        """创建/更新工作区目录和配置。"""
        logger.info("Setting up workspace ~/%s/", REMOTE_DIR)
        ssh.run(f"mkdir -p ~/{REMOTE_DIR}/config ~/{REMOTE_DIR}/sgf ~/{REMOTE_DIR}/output")

        if self._config_file and os.path.exists(self._config_file):
            remote_cfg = f"~/{REMOTE_DIR}/config/analysis_config.cfg"
            if ssh._scp(self._config_file, remote_cfg, timeout=30):
                logger.info("Config copied to workspace")

    # ── 完整部署 ──────────────────────────────────

    def _deploy_full(self, ssh: SshSession, result: DeployResult):
        """完整部署: 创建目录 + 复制二进制/模型/配置 + 验证。"""
        logger.info("Full deploy to %s@%s", result.user, result.host)

        # 创建目录
        ssh.run(f"mkdir -p ~/{REMOTE_DIR}/katago ~/{REMOTE_DIR}/models "
                f"~/{REMOTE_DIR}/config ~/{REMOTE_DIR}/sgf ~/{REMOTE_DIR}/output")

        # 选择 KataGo 版本
        kata_src = self._select_kata(result.env)

        # 复制二进制
        remote_kata = f"~/{REMOTE_DIR}/katago/katago"
        if kata_src and os.path.exists(kata_src):
            logger.info("Copying KataGo: %s → %s", kata_src, remote_kata)
            if ssh._scp(kata_src, remote_kata, timeout=120):
                ssh.run(f"chmod +x {remote_kata}")
                result.kata_path = remote_kata
            else:
                raise RuntimeError(f"Failed to copy KataGo to {remote_kata}")

        remote_kata = result.kata_path

        # 复制模型
        remote_model = f"~/{REMOTE_DIR}/models/{Path(self._model_file).name}"
        if self._model_file and os.path.exists(self._model_file):
            logger.info("Copying model: %s", remote_model)
            if ssh._scp(self._model_file, remote_model, timeout=300):
                result.model_path = remote_model
            else:
                raise RuntimeError(f"Failed to copy model to {remote_model}")

        # 复制配置
        remote_cfg = f"~/{REMOTE_DIR}/config/analysis_config.cfg"
        if self._config_file and os.path.exists(self._config_file):
            if ssh._scp(self._config_file, remote_cfg, timeout=30):
                result.config_path = remote_cfg

        # 验证
        if remote_kata:
            logger.info("Verifying...")
            r = ssh.run(f"{remote_kata} version 2>&1 || echo FAILED")
            if "FAILED" in r.stdout:
                logger.warning("KataGo verification failed: %s", r.stderr[:200])
            else:
                logger.info("KataGo version: %s", r.stdout.strip()[:60])

    # ── 版本选择 ─────────────────────────────────

    def _select_kata(self, env: RemoteEnv) -> Optional[str]:
        """根据远程环境选择 KataGo 二进制。"""
        is_win = "microsoft" in env.os_type or "windows" in env.os_type
        kata_name = Path(self._kata_binary).name.lower() if self._kata_binary else ""

        # 选择策略
        if is_win and "opencl" not in kata_name:
            # Windows 下找 OpenCL exe
            candidates = [
                self._root / "kata-go/windows/katago-v1.16.5-opencl-windows-x64.exe",
                self._root / "kata-go/windows/v1.16.4/katago.exe",
            ]
            for c in candidates:
                if c.exists():
                    return str(c)
            return self._kata_binary

        if not is_win:
            # Linux: CUDA > OpenCL > CPU
            if env.has_cuda:
                cuda_candidates = list(self._root.glob("kata-go/linux/*cuda*"))
                if cuda_candidates:
                    return str(cuda_candidates[0])
            if env.has_opencl:
                ocl_candidates = list(self._root.glob("kata-go/linux/*opencl*"))
                if ocl_candidates:
                    return str(ocl_candidates[0])
            # CPU fallback
            cpu_candidates = list(self._root.glob("kata-go/linux/katago*eigen*"))
            if cpu_candidates:
                return str(cpu_candidates[0])

        return self._kata_binary

    # ── 持久化 ───────────────────────────────────

    def save_to_config(self, result: DeployResult, config_path: str = "config.yaml"):
        """将部署结果写入 config.yaml。"""
        import yaml
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                config = yaml.safe_load(f) or {}
        else:
            config = {}

        hosts = config.setdefault("hosts", [])
        # 去重
        hosts[:] = [h for h in hosts if h.get("name") != result.host]

        entry = {
            "name": result.host,
            "platform": "ssh",
            "host": result.host,
            "port": result.port,
            "user": result.user,
            "kata_path": result.kata_path,
            "model_path": result.model_path,
            "max_concurrent": 3,
            "workspace": result.workspace,
        }
        if result.env:
            entry["env"] = {
                "os": result.env.os_type,
                "cpu": f"{result.env.cpu_model} ({result.env.cpu_cores}c)",
                "gpu": result.env.gpu_model or "",
                "ram_gb": result.env.ram_gb,
            }
        hosts.append(entry)

        with open(path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        logger.info("Saved to %s", config_path)

    # ── 本地资源查找 ─────────────────────────────

    def _find_kata_binary(self) -> Optional[str]:
        candidates = [
            self._root / "kata-go/windows/katago-v1.16.5-opencl-windows-x64.exe",
            self._root / "kata-go/windows/v1.16.4/katago.exe",
            self._root / "kata-go/linux/katago",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    def _find_model(self) -> Optional[str]:
        models_dir = self._root / "kata-go/models"
        if models_dir.exists():
            models = sorted(models_dir.glob("kata1-*.bin.gz"))
            if models:
                return str(models[-1])
        return None

    def _find_config(self) -> Optional[str]:
        candidates = [
            self._root / "kata-go/windows/analysis_config.cfg",
            self._root / "kata-go/windows/v1.16.4/analysis_config.cfg",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return None

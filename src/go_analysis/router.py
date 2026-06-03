"""
AnalysisRouter — 分析路由器。

管理主机注册表、健康检查、智能调度。

用法::

    from go_analysis.router import AnalysisRouter
    from go_analysis.config import ConfigManager

    cfg = ConfigManager()
    router = AnalysisRouter(cfg)
    router.discover_local()
    router.health_check_all()
    best = router.select_best(load=5)
"""

import subprocess
import time
import threading
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

from .config import ConfigManager
from .visits import VisitsStrategy


# ── 数据模型 ────────────────────────────────────────

@dataclass
class HostStatus:
    """主机实时状态"""
    name: str
    platform: str
    kata_path: str
    model_path: Optional[str]
    ssh_host: str = "localhost"
    ssh_port: int = 22
    ssh_user: str = ""
    alive: bool = False
    latency_ms: float = 0.0
    load: int = 0           # 当前排队任务数
    max_concurrent: int = 3
    last_checked: float = 0.0
    error: Optional[str] = None

    @property
    def available_slots(self) -> int:
        return max(0, self.max_concurrent - self.load)

    @property
    def healthy(self) -> bool:
        return self.alive and self.available_slots > 0


# ── Router ────────────────────────────────────────────

class AnalysisRouter:
    """分析路由器 — 主机注册/健康检查/调度"""

    def __init__(self, cfg: ConfigManager):
        self._cfg = cfg
        self._hosts: dict[str, HostStatus] = {}
        self._lock = threading.Lock()
        self._visits_strat: Optional[VisitsStrategy] = None

        # 从配置加载注册主机
        self._load_from_config()

    # ── 注册/注销 ──────────────────────────────────

    def register(self, name: str, platform: str, kata_path: str,
                 model_path: Optional[str] = None,
                 max_concurrent: int = 3,
                 host: str = "localhost", port: int = 0,
                 user: str = ""):
        """注册主机"""
        with self._lock:
            self._hosts[name] = HostStatus(
                name=name, platform=platform,
                kata_path=kata_path, model_path=model_path,
                max_concurrent=max_concurrent,
                ssh_host=host, ssh_port=port, ssh_user=user,
            )

    def unregister(self, name: str):
        """注销主机"""
        with self._lock:
            self._hosts.pop(name, None)

    def discover_local(self, project_root: str | Path = "."):
        """自动发现本机并注册"""
        from .discovery import discover_katago
        hosts = discover_katago(project_root)
        for h in hosts:
            self.register(
                name=h["name"],
                platform=h["platform"],
                kata_path=h["kata_path"],
                model_path=h.get("model_path"),
                max_concurrent=h.get("max_concurrent", 3),
            )
        return len(hosts)

    # ── 读取 ────────────────────────────────────────

    def get(self, name: str) -> Optional[HostStatus]:
        with self._lock:
            return self._hosts.get(name)

    def list_hosts(self) -> list[HostStatus]:
        with self._lock:
            return list(self._hosts.values())

    @property
    def host_count(self) -> int:
        return len(self._hosts)

    # ── 健康检查 ───────────────────────────────────

    def health_check(self, name: str, timeout_s: int = 10) -> HostStatus:
        """检查单个主机"""
        host = self.get(name)
        if not host:
            raise KeyError(f"Unknown host: {name}")

        t0 = time.time()
        try:
            if host.platform == "ssh":
                # SSH host: check remote process via SSH
                from .worker_deploy import SshSession
                ssh = SshSession(host.ssh_host, host.ssh_user, host.ssh_port)
                r = ssh.run(f"dir \"{host.kata_path}\" 2>nul || exit 1", timeout=timeout_s)
                host.alive = r.returncode == 0
                host.error = r.stderr[:200] if r.returncode != 0 else None
            else:
                # Local host: check binary exists
                path = Path(host.kata_path)
                if not path.exists():
                    raise FileNotFoundError(f"KataGo not found: {path}")
                host.alive = True
                host.error = None

        except FileNotFoundError as e:
                result = subprocess.run(
                    [host.kata_path, "version"],
                    capture_output=True, timeout=timeout_s, text=True
                )
                host.alive = result.returncode == 0
                host.error = result.stderr[:200] if result.returncode != 0 else None

        except FileNotFoundError as e:
            host.alive = False
            host.error = str(e)
        except subprocess.TimeoutExpired:
            host.alive = False
            host.error = "Timeout"
        except Exception as e:
            host.alive = False
            host.error = str(e)

        host.latency_ms = (time.time() - t0) * 1000
        host.last_checked = time.time()
        return host

    def health_check_all(self, timeout_s: int = 10) -> list[HostStatus]:
        """检查所有主机"""
        results = []
        for name in list(self._hosts.keys()):
            results.append(self.health_check(name, timeout_s))
        return results

    # ── 调度 ────────────────────────────────────────

    def select_best(self, load: int = 0,
                    preferred_platform: Optional[str] = None) -> Optional[HostStatus]:
        """
        选择最佳主机。

        优先级:
        1. 存活 & 有空闲槽位
        2. preferred_platform 匹配优先
        3. 负载最低
        4. 延迟最低
        """
        candidates = []
        with self._lock:
            for host in self._hosts.values():
                if not host.alive:
                    continue
                if host.available_slots <= 0:
                    continue
                if preferred_platform and host.platform != preferred_platform:
                    continue
                candidates.append(host)

        if not candidates:
            return None

        # 按 负载 < 延迟 排序
        candidates.sort(key=lambda h: (h.load, h.latency_ms))
        return candidates[0]

    def schedule(self, task_size: int = 1,
                 preferred_platform: Optional[str] = None) -> Optional[HostStatus]:
        """
        为任务分配主机, 并递增负载.

        返回分配的主机, 或 None (无可用).
        """
        host = self.select_best(task_size, preferred_platform)
        if host:
            host.load += task_size
        return host

    def release(self, name: str, task_size: int = 1):
        """释放主机负载"""
        host = self.get(name)
        if host:
            host.load = max(0, host.load - task_size)

    # ── 持久化 ──────────────────────────────────────

    def save_to_config(self, filepath: str = "config.yaml"):
        """将当前注册表持久化到 YAML"""
        import yaml
        path = Path(filepath)
        if path.exists():
            with open(path) as f:
                config = yaml.safe_load(f) or {}
        else:
            config = {}

        config["hosts"] = []
        with self._lock:
            for host in self._hosts.values():
                config["hosts"].append({
                    "name": host.name,
                    "platform": host.platform,
                    "host": host.ssh_host,
                    "port": host.ssh_port,
                    "user": host.ssh_user,
                    "kata_path": host.kata_path,
                    "model_path": host.model_path,
                    "max_concurrent": host.max_concurrent,
                })

        with open(path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    def visits_strategy(self) -> VisitsStrategy:
        """获取 visits 策略"""
        if self._visits_strat is None:
            self._visits_strat = VisitsStrategy.from_config(self._cfg)
        return self._visits_strat

    # ── 内部 ────────────────────────────────────────

    def _load_from_config(self):
        """从 ConfigManager 加载主机"""
        # 先尝试从 discovery.py 自动发现
        # (延迟发现, 在第一次 health_check 时扫描)
        from .discovery import discover_katago, register_discovered_hosts
        project_root = Path.cwd()
        hosts = discover_katago(project_root)
        for h in hosts:
            self.register(
                name=h["name"],
                platform=h["platform"],
                kata_path=h["kata_path"],
                model_path=h.get("model_path"),
                max_concurrent=h.get("max_concurrent", 3),
            )

        # 再从配置加载
        for h in self._cfg.hosts:
            if h.get("name") not in self._hosts:
                self.register(
                    name=h["name"],
                    platform=h.get("platform", "auto"),
                    kata_path=h.get("kata_path", ""),
                    model_path=h.get("model_path"),
                    max_concurrent=h.get("max_concurrent", 3),
                    host=h.get("host", "localhost"),
                    port=h.get("port", 22),
                    user=h.get("user", ""),
                )

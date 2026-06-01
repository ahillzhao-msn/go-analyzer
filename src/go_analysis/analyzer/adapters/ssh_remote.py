"""
SSH 远程适配器 — 将任意 SSH 可达主机加入分析池。

工作主机只需: Python 3 + KataGo + 模型文件。
"""

import json
import os
import subprocess
import time
import threading
from typing import Optional
from ..base_adapter import BaseAdapter, AnalysisResult


class SshRemoteAdapter(BaseAdapter):
    """SSH 远程 KataGo 适配器。

    通过 SSH 在远程主机上启动持久 KataGo 分析进程，
    通过 stdin/stdout 管道通信，完全透明于 AnalysisPool。

    Parameters
    ----------
    host : str
        远程主机地址 (IP 或域名)。
    port : int
        SSH 端口，默认 22。
    user : str
        SSH 用户名。
    identity_file : str or None
        SSH 私钥路径。None 则用默认 ~/.ssh/id_*。
    remote_katago : str
        远程主机上 KataGo 的路径。默认检测 PATH。
    remote_model : str
        远程主机上模型文件路径。
    remote_config : str
        远程主机上配置文件路径。None 则自动生成。
    visits : int
        默认访问次数。
    """

    def __init__(self, host: str, port: int = 22, user: str = "root",
                 identity_file: str = None,
                 remote_katago: str = "katago",
                 remote_model: str = "",
                 remote_config: str = "",
                 visits: int = 96):
        self.host = host
        self.port = port
        self.user = user
        self.identity = identity_file or os.path.expanduser("~/.ssh/id_rsa")
        self.remote_katago = remote_katago
        self.remote_model = remote_model
        self.remote_config = remote_config
        self._visits = visits

        self._proc: Optional[subprocess.Popen] = None
        self._running = False
        self._lock = threading.Lock()

    def _ssh_cmd(self, remote_cmd: str) -> list:
        """构建 SSH 命令。"""
        cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            "-p", str(self.port),
        ]
        if os.path.exists(self.identity):
            cmd += ["-i", self.identity]
        cmd += [f"{self.user}@{self.host}", remote_cmd]
        return cmd

    def start(self):
        if self._running:
            return

        # 构建远程 KataGo 命令
        cmd_parts = [self.remote_katago, "analysis"]
        if self.remote_model:
            cmd_parts += ["-model", self.remote_model]
        if self.remote_config:
            cmd_parts += ["-config", self.remote_config]

        remote_cmd = " ".join(cmd_parts)

        ssh_cmd = self._ssh_cmd(remote_cmd)
        self._proc = subprocess.Popen(
            ssh_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._running = True
        time.sleep(3)

        if self._proc.poll() is not None:
            err = self._proc.stderr.read()[:300]
            raise RuntimeError(f"[SSH] {self.host} failed: {err}")

        print(f"[SSH] {self.user}@{self.host} KataGo ready")

    def shutdown(self):
        self._running = False
        if self._proc:
            self._proc.stdin.close()
            self._proc.terminate()
            self._proc.wait(timeout=5)
        self._proc = None
        print(f"[SSH] {self.host} disconnected")

    def is_healthy(self) -> bool:
        if not self._proc or not self._running:
            return False
        return self._proc.poll() is None

    def analyze(self, sgf_content: str, game_id: str = "",
                visits: int = 0, **kwargs) -> AnalysisResult:
        visits = visits or self._visits
        game_id = game_id or f"ssh_{hash(self.host) % 10000}_{int(time.time())}"

        query = json.dumps({
            "id": game_id,
            "sgf": sgf_content,
            "maxVisits": visits,
            "rules": kwargs.get("rules", "chinese"),
            "komi": kwargs.get("komi", 7.5),
            "boardXSize": kwargs.get("board_x", 19),
            "boardYSize": kwargs.get("board_y", 19),
            "includePolicy": True,
        })

        t0 = time.time()
        try:
            with self._lock:
                self._proc.stdin.write(query + "\n")
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
                if not line:
                    raise ConnectionError(f"SSH {self.host} stdout closed")
                response = json.loads(line.strip())
        except Exception as e:
            return AnalysisResult(
                game_id=game_id, success=False,
                error=str(e), duration_s=time.time()-t0,
            )

        move_infos = response.get("moveInfos", [])
        return AnalysisResult(
            game_id=game_id, success=True,
            raw_json=response,
            duration_s=time.time()-t0,
            visits_used=visits,
            move_count=len(move_infos),
        )

    def info(self) -> dict:
        return {"platform": "ssh", "host": self.host,
                "healthy": self.is_healthy(), "visits": self._visits}

    @property
    def platform(self) -> str:
        return f"ssh_{self.host}"

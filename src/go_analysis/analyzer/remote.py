"""RemoteAnalyzer — SSH/HTTP 远程 KataGo 分析器适配器。

封装了两种远程通信协议:
  - SshRemoteAnalyzer: 通过 SSH 调用远程 KataGo
  - HttpRemoteAnalyzer: 通过 HTTP REST API 调用远程 KataGo Worker

注意: 远程分析器只发送 moves 到远端, 远端返回结构化 JSON。
具体 KataGo 进程管理由远程端负责。
"""
import json
import subprocess
import time
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

from .base import BaseAnalyzer, AnalysisResult, extract_12dim_features


class SshRemoteAnalyzer(BaseAnalyzer):
    """SSH 远程 KataGo 分析器。

    通过 SSH 在远程主机上执行 KataGo 分析命令。
    注意: 此实现为简化版, 每局通过 SSH 执行一次完整分析。
    生产环境建议在远程端部署 HttpRemoteAnalyzer 服务。
    """

    def __init__(self, host: str, user: str, katago_path: str,
                 model_path: str, config_path: Optional[str] = None,
                 remote_python: str = "python3",
                 visits: int = 25, timeout: int = 180,
                 identity_file: Optional[str] = None,
                 port: int = 22):
        self.host = host
        self.user = user
        self.port = port
        self.katago_path = katago_path
        self.model_path = model_path
        self.config_path = config_path
        self.remote_python = remote_python
        self.visits = visits
        self.timeout = timeout
        self.identity_file = identity_file

    def analyze(self, moves: list) -> AnalysisResult:
        if not moves:
            return AnalysisResult(success=True, features=[])

        t0 = time.time()
        # 构建远程命令
        config_arg = f" -config {self.config_path}" if self.config_path else ""
        cmd = (f"{self.katago_path} analysis -model {self.model_path}{config_arg}")

        # 构造 JSON 查询并 base64 编码传输
        n = len(moves)
        queries = [
            json.dumps({
                "id": f"g_{i}", "moves": moves[:i],
                "maxVisits": self.visits,
                "rules": "chinese", "komi": 7.5,
                "boardXSize": 19, "boardYSize": 19,
                "includePolicy": True,
            })
            for i in range(n)
        ]
        import base64
        encoded = base64.b64encode(("\n".join(queries)).encode()).decode()
        remote_script = (
            f'echo {encoded} | base64 -d | {cmd}'
        )

        ssh_cmd = ["ssh"]
        if self.identity_file:
            ssh_cmd += ["-i", self.identity_file]
        ssh_cmd += [f"{self.user}@{self.host}", remote_script]

        try:
            result = subprocess.run(
                ssh_cmd, capture_output=True, text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return AnalysisResult(success=False, duration_s=time.time() - t0)
        except Exception:
            return AnalysisResult(success=False, duration_s=time.time() - t0)

        dt = time.time() - t0

        responses = {}
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                parts = r.get("id", "").split("_")
                if parts and parts[-1].isdigit():
                    responses[int(parts[-1])] = r
            except json.JSONDecodeError:
                continue

        if not responses:
            return AnalysisResult(success=False, duration_s=dt)

        features = extract_12dim_features(responses, moves)
        return AnalysisResult(features=features, duration_s=dt, success=True,
                              visits_used=self.visits)

    def benchmark(self, test_moves: list, visits_range: list = None) -> dict:
        return {"best_visits": 25, "note": "benchmark over SSH not supported"}


class HttpRemoteAnalyzer(BaseAnalyzer):
    """HTTP 远程 KataGo 分析器。

    通过 REST API 调用远程 KataGo Worker 服务。
    Worker 端由 distributed.worker 模块提供。
    """

    def __init__(self, endpoint: str, visits: int = 25, timeout: int = 180):
        self.endpoint = endpoint.rstrip("/")
        self.visits = visits
        self.timeout = timeout

    def analyze(self, moves: list) -> AnalysisResult:
        if not moves:
            return AnalysisResult(success=True, features=[])

        t0 = time.time()
        n = len(moves)

        # 构建查询
        queries = [
            {"id": f"g_{i}", "moves": moves[:i],
             "maxVisits": self.visits,
             "rules": "chinese", "komi": 7.5,
             "boardXSize": 19, "boardYSize": 19,
             "includePolicy": True}
            for i in range(n)
        ]

        payload = json.dumps({"queries": queries, "timeout": self.timeout}).encode()
        req = Request(f"{self.endpoint}/analyze", data=payload,
                      headers={"Content-Type": "application/json"})

        try:
            resp = urlopen(req, timeout=self.timeout)
            data = json.loads(resp.read().decode())
        except Exception:
            return AnalysisResult(success=False, duration_s=time.time() - t0)

        dt = time.time() - t0
        responses = {}
        for r in data.get("responses", []):
            parts = r.get("id", "").split("_")
            if parts and parts[-1].isdigit():
                responses[int(parts[-1])] = r

        if not responses:
            return AnalysisResult(success=False, duration_s=dt)

        features = extract_12dim_features(responses, moves)
        return AnalysisResult(features=features, duration_s=dt, success=True,
                              visits_used=self.visits)

    def benchmark(self, test_moves: list, visits_range: list = None) -> dict:
        return {"best_visits": 25, "note": "benchmark over HTTP not supported"}

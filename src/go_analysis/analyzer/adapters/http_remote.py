"""
HTTP 远程适配器 — 通过 REST API 调用远程 KataGo 工作主机。

主控端使用: HttpRemoteAdapter("http://worker:8080")
工作端运行: python -m go_analysis.worker_server
"""

import json
import time
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

from ..base_adapter import BaseAdapter, AnalysisResult


class HttpRemoteAdapter(BaseAdapter):
    """HTTP REST 远程 KataGo 适配器。

    通过 REST API 调用远程工作机上的 KataGo 分析服务。
    工作机只需运行一个轻量 Flask 服务。

    Parameters
    ----------
    endpoint : str
        工作机服务地址 (http://ip:port)。
    timeout : int
        HTTP 请求超时（秒）。
    """

    def __init__(self, endpoint: str, timeout: int = 300):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._healthy = False

    def start(self):
        """检查工作机是否可用。"""
        try:
            req = Request(f"{self.endpoint}/health", method="GET")
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                self._healthy = data.get("status") == "ok"
                print(f"[HTTP] Worker at {self.endpoint} ready: {data.get('info','')}")
        except Exception as e:
            raise RuntimeError(f"[HTTP] Worker {self.endpoint} unreachable: {e}")

    def shutdown(self):
        self._healthy = False
        print(f"[HTTP] {self.endpoint} disconnected")

    def is_healthy(self) -> bool:
        if not self._healthy:
            return False
        try:
            req = Request(f"{self.endpoint}/health", method="GET")
            with urlopen(req, timeout=5) as resp:
                return json.loads(resp.read()).get("status") == "ok"
        except Exception:
            self._healthy = False
            return False

    def analyze(self, sgf_content: str, game_id: str = "",
                visits: int = 0, **kwargs) -> AnalysisResult:
        game_id = game_id or f"http_{hash(self.endpoint) & 0xFFFF}_{int(time.time())}"
        visits = visits or 50

        payload = json.dumps({
            "sgf": sgf_content,
            "game_id": game_id,
            "visits": visits,
            "rules": kwargs.get("rules", "chinese"),
            "komi": kwargs.get("komi", 7.5),
            "board_x": kwargs.get("board_x", 19),
            "board_y": kwargs.get("board_y", 19),
        }).encode()

        t0 = time.time()
        try:
            req = Request(
                f"{self.endpoint}/analyze",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode())
        except URLError as e:
            self._healthy = False
            return AnalysisResult(
                game_id=game_id, success=False,
                error=str(e), duration_s=time.time() - t0,
            )

        return AnalysisResult(
            game_id=game_id,
            success=result.get("success", False),
            raw_json=result.get("data"),
            duration_s=time.time() - t0,
            visits_used=visits,
            move_count=result.get("move_count", 0),
            error=result.get("error", ""),
        )

    def analyze_batch(self, sgf_list: list, visits: int = 50,
                      batch_size: int = 4) -> list:
        """批量分析 — 并行发送多个 HTTP 请求。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = []
        with ThreadPoolExecutor(max_workers=batch_size) as pool:
            futures = {
                pool.submit(self.analyze, sgf, gid, visits): gid
                for sgf, gid in sgf_list
            }
            for fut in as_completed(futures):
                results.append(fut.result())
        return results

    def info(self) -> dict:
        return {"platform": "http", "endpoint": self.endpoint,
                "healthy": self._healthy}

    @property
    def platform(self) -> str:
        return f"http_{self.endpoint}"

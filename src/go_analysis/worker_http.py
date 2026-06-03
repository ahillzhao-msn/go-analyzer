"""
HTTP Worker Server — 轻量版, 不需要 Flask。

在远程主机上启动 HTTP 服务, 接收分析请求。
使用 Python 标准库 http.server.

部署::

    # 远程启动
    python worker_http.py --port 18080 --katago katago.exe

    # 本机注册为 HTTP 主机
    go-analyzer host register --name bob-pc --platform http \\
        --host 192.168.9.31 --port 18080
"""

import json
import os
import subprocess
import time
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse


KATAGO = os.environ.get("KATAGO_PATH",
    "C:/Users/xiaoj/go-analyzer-worker/katago/katago.exe")
MODEL = os.environ.get("KATAGO_MODEL",
    "C:/Users/xiaoj/go-analyzer-worker/models/kata1-b18c384nbt-s6582191360-d3422816034.bin.gz")
CONFIG = os.environ.get("KATAGO_CONFIG",
    "C:/Users/xiaoj/go-analyzer-worker/config/analysis_config.cfg")


class KataGoEngine:
    """管理持久 KataGo 进程."""

    def __init__(self, katago=KATAGO, model=MODEL, config=CONFIG, visits=25):
        self.katago = katago
        self.model = model
        self.config = config
        self.visits = visits
        self._proc = None

    def start(self):
        cmd = [self.katago, "analysis"]
        if self.model:
            cmd += ["-model", self.model]
        if self.config:
            cmd += ["-config", self.config]

        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        time.sleep(2)
        if self._proc.poll() is not None:
            raise RuntimeError("KataGo failed to start")

    def analyze(self, sgf_content, visits=None):
        """发送 SGF 分析。"""
        visits = visits or self.visits
        # Parse moves from SGF
        moves = self._parse_sgf(sgf_content)
        if not moves:
            return {"error": "No moves", "moveInfos": []}

        responses = {}
        for idx in range(len(moves)):
            history = moves[:idx]
            query = json.dumps({
                "id": f"g_{idx}",
                "moves": history,
                "maxVisits": visits,
                "rules": "chinese",
                "komi": 7.5,
                "boardXSize": 19,
                "boardYSize": 19,
            })
            self._proc.stdin.write(query + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            if line:
                resp = json.loads(line.strip())
                rid = resp.get("id", "")
                parts = rid.split("_")
                if parts:
                    try:
                        responses[int(parts[-1])] = resp
                    except ValueError:
                        pass

        return {
            "moveInfos": [r.get("moveInfos", []) for r in responses.values()],
            "responses": responses,
            "move_count": len(moves),
            "analyzed": len(responses),
        }

    def _parse_sgf(self, content):
        moves = []
        i = content.find(";")
        while i >= 0:
            j = content.find(";", i + 1)
            node_str = content[i + 1:j] if j > 0 else content[i + 1:]
            for pl in ("B", "W"):
                idx = node_str.find(f"{pl}[")
                if idx >= 0:
                    end = node_str.find("]", idx)
                    if end > 0:
                        coord = node_str[idx + 2:end]
                        if coord and coord.lower() not in ("tt", "pass", ""):
                            moves.append([pl, coord.upper()])
            i = j
        return moves

    def shutdown(self):
        if self._proc and self._proc.stdin:
            self._proc.stdin.close()
            self._proc.terminate()
            self._proc.wait(timeout=5)


# ── HTTP Handler ──────────────────────────────────────

engine = None


class WorkerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json({"status": "ok", "engine": engine is not None})
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/analyze":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)
            result = engine.analyze(data.get("sgf", ""),
                                     visits=data.get("visits"))
            self._json(result)
        else:
            self._json({"error": "Not found"}, 404)

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass  # quiet


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18080

    global engine
    engine = KataGoEngine()
    engine.start()
    print(f"[Worker] KataGo ready on port {port}")

    server = HTTPServer(("0.0.0.0", port), WorkerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        engine.shutdown()
        print("[Worker] Stopped")


if __name__ == "__main__":
    main()

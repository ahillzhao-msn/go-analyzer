"""
KataGo 工作端 HTTP 服务 — 在远程主机上运行，接收主控的分析请求。

安装: pip install flask
启动: python -m go_analysis.worker_server [--port 8080] [--visits 50]

注册到主控: pool.register_adapter(HttpRemoteAdapter("http://worker_ip:8080"))
"""

import argparse
import json
import os
import sys
import threading
import time

# ── 配置 ────────────────────────────────────────────────

KATAGO = os.environ.get("KATAGO_PATH", "katago")
MODEL = os.environ.get("KATAGO_MODEL", "")
CONFIG = os.environ.get("KATAGO_CONFIG", "")

# 工作目录 (输出日志、tuning 缓存)
os.makedirs("katago_worker_logs", exist_ok=True)
os.chdir("katago_worker_logs")


# ── KataGo 引擎管理器 ──────────────────────────────────

class KataGoEngine:
    """管理持久 KataGo 子进程."""

    def __init__(self, katago_path=KATAGO, model_path=MODEL,
                 config_path=CONFIG, visits=50):
        import subprocess
        self.katago_path = katago_path
        self.model_path = model_path
        self.config_path = config_path
        self.visits = visits
        self._proc = None
        self._lock = threading.Lock()

    def start(self):
        import subprocess
        cmd = [self.katago_path, "analysis"]
        if self.model_path:
            cmd += ["-model", self.model_path]
        if self.config_path:
            cmd += ["-config", self.config_path]

        print(f"[Worker] Start: {cmd[0]} -model ... -config ...")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        time.sleep(3)
        if self._proc.poll() is not None:
            err = self._proc.stderr.read()[:500]
            raise RuntimeError(f"KataGo start failed: {err}")
        print(f"[Worker] KataGo ready (PID={self._proc.pid})")

    def analyze(self, sgf_content, game_id="game", visits=None):
        """分析一局棋谱，返回 JSON 结果。"""
        visits = visits or self.visits
        query = json.dumps({
            "id": game_id,
            "sgf": sgf_content,
            "maxVisits": visits,
            "rules": "chinese",
            "komi": 7.5,
            "boardXSize": 19,
            "boardYSize": 19,
            "includePolicy": True,
        })

        with self._lock:
            self._proc.stdin.write(query + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            if not line:
                raise ConnectionError("KataGo 进程断开")

        return json.loads(line.strip())

    def shutdown(self):
        if self._proc:
            self._proc.stdin.close()
            self._proc.terminate()
            self._proc.wait(timeout=5)
        self._proc = None
        print("[Worker] KataGo closed")


# ── HTTP 服务 ──────────────────────────────────────────

engine = None


def create_app():
    """创建 Flask 应用。"""
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        alive = engine is not None and engine._proc is not None and engine._proc.poll() is None
        return jsonify({
            "status": "ok" if alive else "error",
            "engine_alive": alive,
            "katago": KATAGO,
            "model": os.path.basename(MODEL) if MODEL else "none",
        })

    @app.route("/analyze", methods=["POST"])
    def analyze():
        data = request.get_json()
        if not data or "sgf" not in data:
            return jsonify({"success": False, "error": "缺少 sgf 参数"}), 400

        try:
            result = engine.analyze(
                sgf_content=data["sgf"],
                game_id=data.get("game_id", "worker"),
                visits=data.get("visits", engine.visits),
            )
            move_infos = result.get("moveInfos", [])
            return jsonify({
                "success": True,
                "move_count": len(move_infos),
                "data": result,
            })
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/batch", methods=["POST"])
    def batch():
        """批量分析。"""
        data = request.get_json()
        games = data.get("games", [])
        visits = data.get("visits", engine.visits)
        results = []
        for g in games:
            try:
                r = engine.analyze(g["sgf"], g.get("id", "batch"), visits)
                results.append({"id": g.get("id"), "success": True, "data": r})
            except Exception as e:
                results.append({"id": g.get("id"), "success": False, "error": str(e)})
        return jsonify({"results": results})

    return app


def main():
    parser = argparse.ArgumentParser(description="KataGo 工作端 HTTP 服务")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--katago", default=KATAGO, help="KataGo 路径")
    parser.add_argument("--model", default=MODEL, help="模型路径")
    parser.add_argument("--config", default=CONFIG, help="配置路径")
    parser.add_argument("--visits", type=int, default=50, help="默认 visits")
    args = parser.parse_args()

    global engine
    engine = KataGoEngine(args.katago, args.model, args.config, args.visits)
    engine.start()

    app = create_app()
    print(f"[Worker] HTTP serving on: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()

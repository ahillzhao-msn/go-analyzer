"""WindowsAnalyzer — WSL → Windows KataGo .exe 桥接适配器。

在 WSL 中运行，通过 WSL interop 路径调用 Windows KataGo:
  /mnt/c/Users/.../katago.exe

经验 (ROADMAP.md §4):
  - v1.16.4 OpenCL: 每局独立启动, 3-5s 开销但零死锁
  - WSL 路径转换为 Windows 路径时注意双斜杠
  - OpenCL 版本需要 analysis_config.cfg 指定计算平台
  
注入经验 (来自 distributed.worker 实战):
  - 分步骤 try/except 隔离错误
  - stdin write 失败单独捕获
  - response 解析容错
  - 180s 超时
  - 清理: close → terminate → wait(3) → kill 级联
"""
import json
import platform as _platform
import subprocess
import time
from typing import Optional

from .base import BaseAnalyzer, AnalysisResult, extract_12dim_features


class WindowsAnalyzer(BaseAnalyzer):
    """WSL → Windows KataGo 桥接适配器。"""

    def __init__(self, katago_path: str, model_path: str,
                 config_path: Optional[str] = None,
                 visits: int = 25, timeout: int = 180):
        self.katago_path = katago_path
        self.model_path = model_path
        self.config_path = config_path
        self.visits = visits
        self.timeout = timeout

    def analyze(self, moves: list) -> AnalysisResult:
        """启动 KataGo 分析一个位移序列。"""
        if not moves:
            return AnalysisResult(success=True, features=[])

        t0 = time.time()
        n = len(moves)

        # 1. 启动 KataGo 进程
        try:
            cmd = [self.katago_path, "analysis", "-model", self.model_path]
            if self.config_path:
                cmd += ["-config", self.config_path]
            CREATE_NO_WINDOW = 0x08000000
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            return AnalysisResult(success=False, duration_s=time.time() - t0)

        # 2. 构建 + 发送查询
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
        try:
            proc.stdin.write("\n".join(queries) + "\n")
            proc.stdin.flush()
        except Exception:
            self._cleanup(proc)
            return AnalysisResult(success=False, duration_s=time.time() - t0)

        # 3. 读取响应
        responses = {}
        while len(responses) < n:
            try:
                line = proc.stdout.readline()
                if not line:
                    break
                r = json.loads(line.strip())
                parts = r.get("id", "").split("_")
                if parts and parts[-1].isdigit():
                    responses[int(parts[-1])] = r
            except Exception:
                break
            if time.time() - t0 > self.timeout:
                break

        # 4. 清理进程
        self._cleanup(proc)
        dt = time.time() - t0

        if not responses:
            return AnalysisResult(success=False, duration_s=dt)

        features = extract_12dim_features(responses, moves)
        return AnalysisResult(features=features, duration_s=dt, success=True,
                              visits_used=self.visits)

    def _cleanup(self, proc):
        """安全清理 KataGo 进程。"""
        try:
            proc.stdin.close()
            proc.terminate()
            proc.wait(3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def benchmark(self, test_moves: list, visits_range: list = None) -> dict:
        """基准测试。"""
        if visits_range is None:
            visits_range = [25, 50, 100, 200]
        original = self.visits
        results = []
        for v in visits_range:
            self.visits = v
            t0 = time.time()
            result = self.analyze(test_moves[:min(50, len(test_moves))])
            dt = time.time() - t0
            vps = v * result.num_moves / max(dt, 0.1) if result.success else 0
            results.append({"visits": v, "duration_s": round(dt, 2), "vps": round(vps, 1)})
        self.visits = original
        best = max(results, key=lambda r: r["vps"]) if results else {"visits": self.visits}
        return {"best_visits": best["visits"], "results": results}

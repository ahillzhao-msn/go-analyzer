"""WindowsAnalyzer — WSL → Windows KataGo .exe 桥接适配器。

v0.4.4: 分批查询（batch=50），防止大型棋谱导致管道缓冲区满 + KataGo OpenCL 死锁。
每批独立 KataGo 进程，死锁只影响当前批，超时后自动跳过。
"""
import json
import subprocess
import time
from typing import Optional

from .base import BaseAnalyzer, AnalysisResult, extract_12dim_features


class WindowsAnalyzer(BaseAnalyzer):
    """WSL → Windows KataGo 桥接适配器。每批最多 BATCH 手。"""

    BATCH = 50

    def __init__(self, katago_path: str, model_path: str,
                 config_path: Optional[str] = None,
                 visits: int = 25, timeout: int = 180):
        self.katago_path = katago_path
        self.model_path = model_path
        self.config_path = config_path
        self.visits = visits
        self.timeout = timeout

    def _analyze_batch(self, moves: list, offset: int) -> AnalysisResult:
        """分析一批 (≤BATCH 手) 位移。"""
        if not moves:
            return AnalysisResult(success=True, features=[])

        n = len(moves)
        t0 = time.time()

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

        # 2. 构建查询（相对于偏移量构建完整棋盘状态）
        queries = [
            json.dumps({
                "id": f"g_{offset + i}", "moves": moves[:i],
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

        # 3. 读取响应（带超时）
        responses = {}
        deadline = time.time() + self.timeout

        while len(responses) < n and time.time() < deadline:
            try:
                line = proc.stdout.readline()
                if not line:
                    break
                r = json.loads(line.strip())
                parts = r.get("id", "").split("_")
                if parts and parts[-1].isdigit():
                    idx = int(parts[-1])
                    responses[idx] = r
            except Exception:
                continue

        # 4. 清理
        self._cleanup(proc)
        dt = time.time() - t0

        if not responses:
            return AnalysisResult(success=False, duration_s=dt)

        # 提取特征（只保留本批的）
        feats = extract_12dim_features(responses, moves)
        return AnalysisResult(features=feats, duration_s=dt, success=True,
                              visits_used=self.visits)

    def analyze(self, moves: list) -> AnalysisResult:
        """分析一局棋谱，分批发送查询防止死锁传播。"""
        if not moves:
            return AnalysisResult(success=True, features=[])

        n = len(moves)
        all_features = []
        total_dt = 0.0

        for start in range(0, n, self.BATCH):
            batch = moves[start:start + self.BATCH]
            result = self._analyze_batch(batch, start)
            total_dt += result.duration_s
            if not result.success:
                return AnalysisResult(
                    features=all_features if all_features else [],
                    duration_s=total_dt,
                    success=len(all_features) > 0,
                    visits_used=self.visits,
                )
            all_features.append(result.features)

        import numpy as np
        combined = np.concatenate(all_features, axis=0) if len(all_features) > 1 else all_features[0]
        return AnalysisResult(features=combined, duration_s=total_dt,
                              success=True, visits_used=self.visits)

    def _cleanup(self, proc):
        """安全清理 KataGo 进程。"""
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(3)
        except Exception:
            try:
                proc.kill()
                proc.wait(1)
            except Exception:
                pass
        try:
            proc.stdin.close()
        except Exception:
            pass

    def benchmark(self, test_moves: list, visits_range: list = None) -> dict:
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

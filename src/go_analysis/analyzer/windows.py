"""WindowsAnalyzer — WSL → Windows KataGo 分块流式适配器 (v0.4.4)。

分块流式: 分批发送查询（默认 50 手/批），每批读完回复再发下一批。
管道缓冲区永不撑爆，KataGo 持续处理不停顿。

策略:
  - batch=50: 每批 ~10KB 查询数据 (50 × ~200 bytes) → 远低于 Windows 64KB 管道上限
  - 逐批读取: 写完一批立刻读回复 → 管道畅通 → KataGo 无积压
  - 每局独立进程: Worker 的常驻管理决定何时重启
"""
import json
import subprocess
import time
from typing import Optional

from .base import BaseAnalyzer, AnalysisResult, extract_12dim_features

CREATE_NO_WINDOW = 0x08000000
BATCH = 50  # 每批手数: ~10KB, 安全低于管道上限


class WindowsAnalyzer(BaseAnalyzer):
    """WSL → Windows KataGo 分块流式适配器。"""

    def __init__(self, katago_path: str, model_path: str,
                 config_path: Optional[str] = None,
                 visits: int = 25, timeout: int = 180):
        self.katago_path = katago_path
        self.model_path = model_path
        self.config_path = config_path
        self.visits = visits
        self.timeout = timeout

    def analyze(self, moves: list) -> AnalysisResult:
        """分块流式分析。"""
        if not moves:
            return AnalysisResult(success=True, features=[])

        n = len(moves)
        t0 = time.time()
        proc = None
        all_features = []

        try:
            # 1. 启动 KataGo 进程 (每局一个)
            cmd = [self.katago_path, "analysis", "-model", self.model_path]
            if self.config_path:
                cmd += ["-config", self.config_path]
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )

            deadline = time.time() + self.timeout

            # 2. 分块: 每批 BATCH 手, 写完即读
            for batch_start in range(0, n, BATCH):
                batch_end = min(batch_start + BATCH, n)
                batch_moves = moves[:batch_end]  # 完整棋局到当前手

                # 发送本批查询 (batch_start ~ batch_end-1)
                queries = []
                for i in range(batch_start, batch_end):
                    queries.append(json.dumps({
                        "id": f"g_{i}",
                        "moves": moves[:i],
                        "maxVisits": self.visits,
                        "rules": "chinese", "komi": 7.5,
                        "boardXSize": 19, "boardYSize": 19,
                        "includePolicy": True,
                    }))
                proc.stdin.write("\n".join(queries) + "\n")
                proc.stdin.flush()

                # 读取本批回复 (带超时)
                expected = batch_end - batch_start
                got = 0
                while got < expected and time.time() < deadline:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    try:
                        r = json.loads(line.strip())
                        rid = r.get("id", "")
                        if rid.startswith("g_") and rid[2:].isdigit():
                            idx = int(rid[2:])
                            if len(all_features) <= idx:
                                all_features.extend([None] * (idx + 1 - len(all_features)))
                            all_features[idx] = r
                            got += 1
                    except Exception:
                        continue

                if got < expected:
                    break  # 超时或进程挂了

        except Exception:
            pass
        finally:
            if proc:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                try:
                    proc.wait(3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        dt = time.time() - t0

        # 过滤 None, 提取特征
        valid = [r for r in all_features if r is not None]
        if not valid:
            return AnalysisResult(success=False, duration_s=dt)

        import numpy as np
        feats = extract_12dim_features(
            {i: r for i, r in enumerate(valid)}, moves[:len(valid)]
        )
        return AnalysisResult(features=feats, duration_s=dt, success=True,
                              visits_used=self.visits)

    def benchmark(self, test_moves: list, visits_range: list = None) -> dict:
        """基准测试 — 找出最优 visits 配置。"""
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

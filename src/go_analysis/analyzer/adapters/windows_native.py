"""
Windows 原生适配器 — WSL → Windows Katago.exe 桥接。

利用 v1.16.4 OpenCL Windows KataGo。持久进程 + 逐手分析模式。
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from ..base_adapter import BaseAdapter, AnalysisResult
from ..protocol import AnalysisProtocol


# ── 路径常量 (环境变量覆盖) ─────────────────────────────

import os as _os
_PROJ = _os.environ.get('GO_ANALYSIS_PROJ', _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_WIN_PROJ = _os.environ.get('GO_ANALYSIS_WIN_PROJ', _PROJ.replace('/mnt/c/', 'C:/').replace('/', '\\'))

_DEFAULT_KATAGO_EXE = _os.path.join(_PROJ, 'kata-go', 'windows', 'v1.16.4', 'katago.exe')
_DEFAULT_MODEL = _os.path.join(_WIN_PROJ, 'kata-go', 'models', 'kata1-b18c384nbt-s6582191360-d3422816034.bin.gz')
_DEFAULT_CONFIG = _os.path.join(_WIN_PROJ, 'kata-go', 'windows', 'analysis_config.cfg')
_DEFAULT_DLL_DIR = _os.path.join(_PROJ, 'kata-go', 'windows', 'v1.16.4')


class WindowsNativeAdapter(BaseAdapter):
    """Windows 原生 KataGo 适配器 (持久进程版)。

    特性:
    - 持久 KataGo 子进程 (避免重复启动开销)
    - 自动逐手分析一局棋的所有位置
    - 健康检查 + 自动重连
    - 已缓存 OpenCL tuning 加速
    """

    def __init__(
        self,
        kata_path: str = None,
        model_path: str = None,
        config_path: str = None,
        dll_dir: str = None,
        visits: int = 96,
        num_analysis_threads: int = 2,
        num_search_threads: int = 16,
    ):
        self._visits = visits
        self._kata_path = kata_path or _DEFAULT_KATAGO_EXE
        self._model_path = model_path or _DEFAULT_MODEL
        self._config_path = config_path or _DEFAULT_CONFIG
        self._dll_dir = dll_dir or _DEFAULT_DLL_DIR

        self._threads_setting = {
            "numAnalysisThreads": num_analysis_threads,
            "numSearchThreads": num_search_threads,
        }

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._running = False
        self._req_counter = 0

    # ── 生命周期 ────────────────────────────────────────

    def start(self):
        if self._running:
            return

        # 确保 DLL 在 exe 同目录
        dll_src = Path(self._dll_dir)
        if dll_src.exists():
            for dll in dll_src.glob("*.dll"):
                target = Path(self._kata_path).parent / dll.name
                if not target.exists():
                    import shutil
                    shutil.copy2(dll, target)

        # 确保配置文件存在
        cfg_path = self._ensure_config()

        self._stderr_log = open(
            os.path.join(os.path.dirname(self._kata_path), "adapter_stderr.log"), "a"
        )
        self._proc = subprocess.Popen(
            [self._kata_path, "analysis", "-model", self._model_path, "-config", cfg_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_log,
            text=True,
            bufsize=1,
        )
        self._running = True
        self._req_counter = 0

        # 等待初始化 (含 OpenCL tuning)
        time.sleep(3)
        if self._proc.poll() is not None:
            err = self._proc.stderr.read()[:500]
            raise RuntimeError(
                f"KataGo crashed on startup (code {self._proc.returncode}): {err}"
            )

        print(f"[WinAdapter] Started PID={self._proc.pid} visits={self._visits}")

    def _ensure_config(self) -> str:
        """确保 KataGo 配置文件存在，返回 Windows 路径。"""
        cfg_local = self._config_path.replace(_WIN_PROJ, _PROJ).replace("\\", "/")
        if not os.path.exists(cfg_local):
            cfg_dir = os.path.dirname(cfg_local)
            os.makedirs(cfg_dir, exist_ok=True)
            with open(cfg_local, "w") as f:
                f.write(
                    "logDir = analysis_logs\n"
                    "reportAnalysisWinratesAs = BLACK\n"
                    "analysisPVLen = 15\n"
                    "wideRootNoise = 0.04\n"
                    f"numAnalysisThreads = {self._threads_setting['numAnalysisThreads']}\n"
                    f"numSearchThreads = {self._threads_setting['numSearchThreads']}\n"
                    "nnMaxBatchSize = 8\n"
                )
        return self._config_path

    def shutdown(self):
        self._running = False
        if self._proc and self._proc.stdin:
            self._proc.stdin.close()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None
        try:
            self._stderr_log.close()
        except Exception:
            pass
        print("[WinAdapter] Shutdown")

    def is_healthy(self) -> bool:
        if not self._proc or not self._running:
            return False
        poll = self._proc.poll()
        if poll is not None:
            return False
        # 尝试发送一个空查询检查进程活性
        try:
            with self._lock:
                self._proc.stdin.write(
                    '{"id":"ping","moves":[],"maxVisits":1,' +
                    '"rules":"chinese","komi":7.5,"boardXSize":19,"boardYSize":19}\n'
                )
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
                return bool(line)
        except Exception:
            return False

    # ── 核心分析 ────────────────────────────────────────

    def analyze(self, sgf_content: str, game_id: str = "",
                visits: int = 0, **kwargs) -> AnalysisResult:
        """分析一局棋谱（逐手分析所有位置）。

        将 SGF 解析为 moves 数组后，对每个位置发送查询。
        """
        if not self._running:
            self.start()

        visits = visits or self._visits
        game_id = game_id or f"g{self._req_counter}"
        self._req_counter += 1

        # 解析 SGF → moves
        try:
            all_moves = AnalysisProtocol.sgf_to_moves(sgf_content)
        except Exception as e:
            return AnalysisResult(
                game_id=game_id, success=False,
                error=f"SGF parse failed: {e}",
            )

        if not all_moves:
            return AnalysisResult(
                game_id=game_id, success=False,
                error="No valid moves found in SGF",
            )

        total_moves = len(all_moves)
        start_time = time.time()

        # 批量提交: 一次性写入所有位置查询
        # KataGo 利用 numAnalysisThreads 并行处理
        queries = []
        for query_idx in range(total_moves):
            history = all_moves[:query_idx]
            query = AnalysisProtocol.build_query(
                f"{game_id}_{query_idx}", history, visits,
            )
            queries.append(query)

        # 写入 stdin
        with self._lock:
            for q in queries:
                self._proc.stdin.write(q + "\n")
            self._proc.stdin.flush()

        # 流式读取所有响应
        raw_responses = {}
        completed = 0
        last_report = 0

        # 读取线程
        import queue, threading
        resp_queue = queue.Queue()
        read_done = threading.Event()

        def _reader():
            for _ in range(total_moves):
                try:
                    line = self._proc.stdout.readline()
                    if line:
                        resp_queue.put(json.loads(line.strip()))
                except Exception:
                    break
            read_done.set()

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        # 主线程收集响应
        while completed < total_moves and not read_done.is_set():
            try:
                response = resp_queue.get(timeout=1)
                resp_id = response.get("id", "")
                # Extract query_idx from id like "game_42"
                parts = resp_id.split("_")
                if parts:
                    idx = int(parts[-1])
                    raw_responses[idx] = response
                    completed += 1

                    # 进度报告
                    if completed - last_report >= 50:
                        elapsed = time.time() - start_time
                        rate = completed / elapsed
                        eta = (total_moves - completed) / rate
                        print(f"  {completed}/{total_moves} ({rate:.1f}q/s, ETA {eta:.0f}s)")
                        last_report = completed
            except queue.Empty:
                if completed >= total_moves:
                    break

        reader_thread.join(timeout=5)

        # 提取特征
        from ...models import compute_global_stats
        import numpy as np

        features_list = AnalysisProtocol.extract_features_from_moves(
            all_moves, raw_responses
        )

        if not features_list:
            return AnalysisResult(
                game_id=game_id, success=False,
                error="No features extracted",
                duration_s=time.time() - start_time,
            )

        # 构建特征矩阵和全局统计
        feats = np.stack([f["features"] for f in features_list], axis=0)
        gs = compute_global_stats(
            [type("obj", (object,), {"features": f["features"]})()
             for f in features_list]
        )

        duration = time.time() - start_time

        return AnalysisResult(
            game_id=game_id,
            success=True,
            raw_json={
                "features_shape": list(feats.shape),
                "move_count": total_moves,
                "positions_analyzed": len(features_list),
                "features_list": features_list,
            },
            duration_s=duration,
            visits_used=visits,
            move_count=total_moves,
        )

    def analyze_batch(self, sgf_list: list, visits: int = 96,
                      batch_size: int = 1) -> list:
        """批量分析（串行：每局使用同一持久进程）。"""
        results = []
        for sgf_content, game_id in sgf_list:
            result = self.analyze(sgf_content, game_id, visits)
            results.append(result)
        return results

    # ── 信息 ────────────────────────────────────────────

    def info(self) -> dict:
        return {
            "platform": self.platform,
            "kata_version": "v1.16.4",
            "model": os.path.basename(self._model_path.replace("\\", "/")),
            "visits": self._visits,
            "healthy": self.is_healthy(),
        }

    @property
    def platform(self) -> str:
        return "windows_native"

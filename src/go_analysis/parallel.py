"""
ParallelEngine — 并行 KataGo 分析引擎。

同时运行多个 KataGo 进程, 自动分配 SGF 任务。

使用::

    from go_analysis.parallel import ParallelEngine
    from go_analysis.config import ConfigManager

    cfg = ConfigManager()
    engine = ParallelEngine(cfg)
    engine.start()

    # 提交分析任务
    results = engine.analyze_all(sgf_files)

    engine.shutdown()
"""

import time
import threading
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import ConfigManager
from .analyzer import create_adapter
from .visits import VisitsStrategy, smart_visits
from .monitor import AnalysisMonitor


class ParallelEngine:
    """
    并行分析引擎.

    管理多个 KataGo 进程, 自动分配任务和 visits.
    """

    def __init__(self, cfg: ConfigManager):
        self._cfg = cfg
        self._num_engines = cfg.get("analyzer.parallel_engines", 1)
        self._gpu_devices = cfg.get("analyzer.gpu_devices", [0])
        self._visits_strat = VisitsStrategy.from_config(cfg)
        self._adapter = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._monitor = AnalysisMonitor()

    @property
    def monitor(self) -> AnalysisMonitor:
        return self._monitor

    def start(self):
        """启动引擎"""
        self._adapter = create_adapter(
            platform=self._cfg.get("analyzer.default_platform", "auto"),
            visits=self._cfg.get("analyzer.visits", 96),
        )
        self._adapter.start()
        self._pool = ThreadPoolExecutor(max_workers=self._num_engines)

    def shutdown(self):
        """关闭引擎"""
        if self._pool:
            self._pool.shutdown(wait=True)
        if self._adapter:
            self._adapter.shutdown()

    # ── 单局分析 (含 SmartVisits) ──────────────────

    def analyze_one(self, sgf_path: str, game_id: str = "",
                    visits: int = 0) -> Optional[dict]:
        """
        分析一盘棋, 按游戏阶段动态分配 visits.

        Returns
        -------
        dict or None
            {"game_id", "move_count", "total_visits", "total_time_s",
             "avg_vps", "moves": [...]}
        """
        path = Path(sgf_path)
        game_id = game_id or path.stem
        content = path.read_text(encoding="utf-8", errors="replace")

        self._monitor.start_game(game_id)

        # 粗解析总手数
        total_moves = content.count(";B[") + content.count(";W[")
        self._current_game_total = total_moves

        # 逐步分析
        all_moves_results = []
        try:
            for move_num in range(1, total_moves + 1):
                # SmartVisits: 根据棋步位置选择 visits
                actual_visits = visits or self._visits_strat.get_visits(
                    move_num, total_moves
                )

                # 分析
                t0 = time.time()
                result = self._adapter.analyze(content, game_id=game_id,
                                                visits=actual_visits)
                dt = time.time() - t0

                # 记录
                phase = self._classify_phase(move_num, total_moves)
                self._monitor.record_move(move_num, actual_visits, dt, phase)

                if result and result.success:
                    all_moves_results.append({
                        "move_num": move_num,
                        "visits": actual_visits,
                        "time_s": round(dt, 2),
                        "vps": round(actual_visits / dt, 1) if dt > 0 else 0,
                        "phase": phase,
                    })

        except Exception as e:
            timing = self._monitor.finish_game()
            return {"game_id": game_id, "error": str(e), "success": False}

        timing = self._monitor.finish_game()
        return {
            "game_id": game_id,
            "move_count": total_moves,
            "total_visits": timing.total_visits,
            "total_time_s": round(timing.total_duration_s, 2),
            "avg_vps": round(timing.avg_vps, 1),
            "bottleneck": timing.bottleneck_hint,
            "moves": all_moves_results,
            "success": True,
        }

    # ── 批量分析 ──────────────────────────────────

    def analyze_all(self, sgf_files: list[str],
                    visits: int = 0,
                    max_workers: int = 0) -> list[dict]:
        """
        批量分析多盘棋.

        使用线程池并行.
        """
        if max_workers <= 0:
            max_workers = self._num_engines

        if max_workers <= 1:
            # 串行
            results = []
            for sgf in sgf_files:
                results.append(self.analyze_one(sgf, visits=visits))
            return results

        # 并行
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self.analyze_one, sgf, visits=visits): sgf
                for sgf in sgf_files
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                except Exception as e:
                    results.append({"error": str(e), "success": False})

        return results

    # ── 内部 ──────────────────────────────────────

    @staticmethod
    def _classify_phase(move_num: int, total: int) -> str:
        if total <= 100:
            return "opening" if move_num <= total * 0.3 else "midgame"
        if move_num <= 60:
            return "opening"
        if move_num <= total * 0.85:
            return "midgame"
        return "endgame"

"""Pipeline — SGF → NPZ 分析管线。

编排器核心:
  1. Source 获取 SGF
  2. sgf_parser 提取主线
  3. Analyzer 分析位移
  4. 采集环境信息
  5. 封装 AnalysisRecord
  6. Store 保存 NPZ
"""
import time
import logging
from typing import Optional

from ..data.format import AnalysisRecord, GameMeta, HardwareEnv, SoftwareEnv
from ..data.source import BaseSource
from ..data.store import BaseStore
from ..analyzer import BaseAnalyzer, AnalysisResult
from .sgf_parser import extract_main_line, count_main_line
from .environment import collect_hardware, collect_software, extract_game_meta_from_sgf

log = logging.getLogger("pipeline")


class Pipeline:
    """SGF → NPZ 分析管线。

    Usage:
        pipeline = Pipeline(analyzer, source, store)
        stats = pipeline.run_all()  # 分析全部
        result = pipeline.run_one("game-001")  # 分析单谱
    """

    def __init__(self, analyzer: BaseAnalyzer,
                 source: BaseSource,
                 store: BaseStore,
                 visits: int = 25,
                 min_moves: int = 50):
        self.analyzer = analyzer
        self.source = source
        self.store = store
        self.visits = visits
        self.min_moves = min_moves

    def run_one(self, game_id: str) -> dict:
        """分析一局棋谱。

        Returns:
            {"status": "ok"|"skip"|"fail", "moves": N, "duration_s": X, "path": str}
        """
        t0 = time.time()

        # 1. 跳过已存在的
        if self.store.exists(game_id):
            return {"status": "skip_exists", "game_id": game_id,
                    "duration_s": time.time() - t0}

        # 2. 获取 SGF
        try:
            sgf_content, raw_meta = self.source.get_game(game_id)
        except KeyError:
            return {"status": "fail", "game_id": game_id,
                    "error": "source_not_found", "duration_s": time.time() - t0}

        # 3. 提取主线
        moves = extract_main_line(sgf_content)
        if not moves:
            return {"status": "fail", "game_id": game_id,
                    "error": "no_moves", "duration_s": time.time() - t0}

        # 4. 验证最少手数
        if len(moves) < self.min_moves:
            return {"status": "skip", "game_id": game_id,
                    "moves": len(moves), "reason": "too_short",
                    "duration_s": time.time() - t0}

        # 5. Analyzer 分析
        result = self.analyzer.analyze(moves)
        if not result.success or result.num_moves == 0:
            return {"status": "fail", "game_id": game_id,
                    "moves": len(moves), "error": "analysis_failed",
                    "duration_s": time.time() - t0}

        # 6. 采集环境
        try:
            hw = collect_hardware()
            sw = collect_software()
            meta = extract_game_meta_from_sgf(sgf_content, game_id)
            # 合并 raw_meta 中的信息
            if raw_meta:
                for k, v in raw_meta.items():
                    if hasattr(meta, k) and v is not None:
                        setattr(meta, k, v)
        except Exception:
            hw = HardwareEnv()
            sw = SoftwareEnv()
            meta = GameMeta(game_id=game_id)

        meta.move_count = len(moves)

        # 7. 封装 AnalysisRecord
        record = AnalysisRecord(
            game_id=game_id,
            features=result.features,
            metadata=meta,
            hardware_env=hw,
            software_env=sw,
            visits_used=self.visits,
            analysis_duration_s=time.time() - t0,
        )

        # 8. 保存
        path = self.store.save(game_id, record)

        dt = time.time() - t0
        return {"status": "ok", "game_id": game_id,
                "moves": len(moves), "path": path,
                "duration_s": round(dt, 2)}

    def run_all(self, resume: bool = True) -> dict:
        """分析来源中所有未完成的棋谱。

        Args:
            resume: True → 跳过已有的; False → 全部重新分析

        Returns: {"total": N, "ok": N, "skip": N, "fail": N, "duration_s": X}
        """
        games = self.source.list_games()
        stats = {"total": len(games), "ok": 0, "skip": 0, "fail": 0,
                 "skip_exists": 0, "moves_total": 0, "duration_s": 0}

        for i, game_id in enumerate(games):
            result = self.run_one(game_id)
            stats[result["status"]] = stats.get(result["status"], 0) + 1
            stats["duration_s"] += result.get("duration_s", 0)
            stats["moves_total"] += result.get("moves", 0)

            if (i + 1) % 100 == 0:
                log.info(f"Progress: {i+1}/{stats['total']} "
                         f"(ok={stats['ok']}, skip={stats['skip']}, fail={stats['fail']})")

        stats["duration_s"] = round(stats["duration_s"], 2)
        return stats

    def resume(self) -> dict:
        """从中断处继续 (等效 run_all(resume=True))。"""
        return self.run_all(resume=True)

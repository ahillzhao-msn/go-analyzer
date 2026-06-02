"""
AnalysisMonitor — 分析性能监控。

追踪每次分析的耗时、visits、瓶颈诊断。

使用::

    from go_analysis.monitor import AnalysisMonitor

    monitor = AnalysisMonitor()
    monitor.start_game("game001", total_moves=250)

    for move_num in range(250):
        with monitor.measure_move(move_num):
            result = adapter.analyze(...)
            monitor.record_move(move_num, result.visits_used, result.duration_s)

    report = monitor.summary()
    print(report["bottleneck_hint"])
"""

import time
from dataclasses import dataclass, field
from typing import Optional
from collections import deque


@dataclass
class MoveTiming:
    """单步分析记录"""
    move_num: int
    visits: int
    duration_s: float
    phase: str = ""
    speed_vps: float = 0.0  # visits per second


@dataclass
class GameTiming:
    """整局分析记录"""
    game_id: str = ""
    total_moves: int = 0
    total_visits: int = 0
    total_duration_s: float = 0.0
    moves: list[MoveTiming] = field(default_factory=list)
    avg_vps: float = 0.0
    bottleneck_hint: str = ""


class AnalysisMonitor:
    """分析性能监控器"""

    def __init__(self, window_size: int = 20):
        self._current: Optional[GameTiming] = None
        self._move_start: float = 0.0
        self._history: deque[GameTiming] = deque(maxlen=100)
        self._window_size = window_size

    # ── 游戏生命周期 ──────────────────────────────

    def start_game(self, game_id: str, total_moves: int = 0):
        """开始记录一局分析"""
        self._current = GameTiming(
            game_id=game_id,
            total_moves=total_moves,
        )

    def finish_game(self) -> Optional[GameTiming]:
        """结束当前局, 返回汇总"""
        timing = self._current
        if timing and timing.total_duration_s > 0:
            timing.avg_vps = timing.total_visits / timing.total_duration_s
            timing.bottleneck_hint = self._diagnose(timing)
            self._history.append(timing)
        self._current = None
        return timing

    # ── 单步记录 ──────────────────────────────────

    def measure_move(self, move_num: int):
        """上下文管理器: 记录单步耗时"""
        self._move_start = time.time()
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def record_move(self, move_num: int, visits: int, duration_s: float,
                    phase: str = ""):
        """记录单步分析结果"""
        if self._current is None:
            return
        speed = visits / duration_s if duration_s > 0 else 0
        timing = MoveTiming(
            move_num=move_num,
            visits=visits,
            duration_s=duration_s,
            phase=phase,
            speed_vps=speed,
        )
        self._current.moves.append(timing)
        self._current.total_visits += visits
        self._current.total_duration_s += duration_s

    # ── 聚合 ──────────────────────────────────────

    def summary(self) -> dict:
        """当前局汇总"""
        timing = self._current
        if timing is None:
            return {}
        if timing.total_duration_s > 0:
            timing.avg_vps = timing.total_visits / timing.total_duration_s
            timing.bottleneck_hint = self._diagnose(timing)
        return {
            "game_id": timing.game_id,
            "moves": len(timing.moves),
            "total_visits": timing.total_visits,
            "total_time_s": round(timing.total_duration_s, 2),
            "avg_vps": round(timing.avg_vps, 1),
            "bottleneck": timing.bottleneck_hint,
            "per_move": [
                {
                    "move": m.move_num,
                    "visits": m.visits,
                    "time_s": round(m.duration_s, 2),
                    "vps": round(m.speed_vps, 1),
                    "phase": m.phase,
                }
                for m in timing.moves[-10:]  # 只看最后10步
            ],
        }

    def history(self, n: int = 10) -> list[dict]:
        """最近 N 局汇总"""
        return [
            {
                "game_id": g.game_id,
                "moves": len(g.moves),
                "total_time_s": round(g.total_duration_s, 2),
                "avg_vps": round(g.avg_vps, 1),
                "bottleneck": g.bottleneck_hint,
            }
            for g in list(self._history)[-n:]
        ]

    # ── 瓶颈诊断 ───────────────────────────────────

    def _diagnose(self, timing: GameTiming) -> str:
        """诊断性能瓶颈"""
        if not timing.moves:
            return "no data"

        vps_values = [m.speed_vps for m in timing.moves if m.speed_vps > 0]
        if not vps_values:
            return "no speed data"

        avg_vps = sum(vps_values) / len(vps_values)

        # 检测慢步
        slow_moves = [m for m in timing.moves
                      if m.speed_vps < avg_vps * 0.5]
        if len(slow_moves) > len(timing.moves) * 0.3:
            return f"大量慢步({len(slow_moves)}/{len(timing.moves)}), 平均vps={avg_vps:.0f}, 建议降低visits或增加search_threads"

        # 检测首步延迟 (KataGo 加载)
        if timing.moves and timing.moves[0].duration_s > 5.0:
            return f"首次分析延迟高({timing.moves[0].duration_s:.1f}s), 考虑预热"

        # 检测趋势 (越分析越慢?)
        if len(vps_values) >= 20:
            first_half = sum(vps_values[:len(vps_values)//2]) / (len(vps_values)//2)
            second_half = sum(vps_values[-len(vps_values)//2:]) / (len(vps_values)//2)
            if second_half < first_half * 0.7:
                return f"速度下降({first_half:.0f}→{second_half:.0f} vps), 可能GPU过热或内存碎片"

        return f"正常, avg {avg_vps:.0f} vps"

    def print_report(self):
        """打印可读报告"""
        s = self.summary()
        if not s:
            return "No active game"
        lines = [
            f"Game: {s['game_id']}",
            f"  Moves: {s['moves']}",
            f"  Total visits: {s['total_visits']}",
            f"  Total time:   {s['total_time_s']}s",
            f"  Avg speed:    {s['avg_vps']} vps",
            f"  Bottleneck:   {s['bottleneck']}",
        ]
        if s.get("per_move"):
            lines.append("  Last moves:")
            for m in s["per_move"][-5:]:
                lines.append(f"    {m['move']:>4d}: {m['visits']}v in {m['time_s']}s ({m['vps']} vps)")
        return "\n".join(lines)

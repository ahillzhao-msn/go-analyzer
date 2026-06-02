"""
SmartVisits — 智能 visits 分配。

核心思想: 不是每一步都需要同等分析预算。

- 序盘 (1-60): 高频, 关键变化多 → 高 visits
- 中盘战斗 (61-200): 关键步确定后, 低 visits 跑试探 → 差异分配
- 官子 (201+): 收敛, 低 visits

访问模式:

    smart_visits(move_mum=25, total=250)  → 96  (序盘重要)
    smart_visits(move_mum=150, total=250) → 50  (中盘)
    smart_visits(move_mum=230, total=250) → 25  (官子)

也支持:

    - prototype: 25 visits 快速验证
    - batch: 50 visits 批量处理
    - precision: 96 visits 精确分析
    - custom: 指定 profile

使用::

    from go_analysis.visits import smart_visits, VisitsStrategy
    from go_analysis.config import ConfigManager

    cfg = ConfigManager()
    strat = VisitsStrategy.from_config(cfg)

    for move_num in range(total_moves):
        v = strat.get_visits(move_num, total_moves)
        # analyze with v visits
"""

from typing import Optional
from .config import ConfigManager


# ── Profile 定义 ─────────────────────────────────────

PROFILES = {
    "prototype": {
        "base": 25,
        "opening_mult": 1.2,     # 序盘 30
        "midgame_std": 25,        # 中盘基线
        "endgame_std": 15,        # 官子
        "critical_mult": 1.5,     # 关键步 38
    },
    "batch": {
        "base": 50,
        "opening_mult": 1.4,     # 序盘 70
        "midgame_std": 50,
        "endgame_std": 25,
        "critical_mult": 1.8,    # 关键步 90
    },
    "precision": {
        "base": 96,
        "opening_mult": 1.0,     # 序盘 = base
        "midgame_std": 96,
        "endgame_std": 48,
        "critical_mult": 2.0,    # 关键步 192
    },
}


# ── 关键步检测 ──────────────────────────────────────

def is_critical_move(move_num: int, total: int,
                     prev_eval_change: Optional[float] = None) -> bool:
    """判断是否关键步"""
    # 模式 1: 局部争棋 (包含战斗、攻杀)
    if 60 <= move_num <= 200:
        return move_num % 5 == 0  # 每5步采样一次高精度

    # 模式 2: 终局前的大转换
    if total > 200 and move_num >= total - 40:
        return False

    # 模式 3: 胜率剧烈变化 (需要上游传入 prev_eval_change)
    if prev_eval_change is not None and abs(prev_eval_change) > 0.15:
        return True

    return False


# ── Phase 检测 ──────────────────────────────────────

def game_phase(move_num: int, total: int) -> str:
    """返回 'opening' | 'midgame' | 'endgame'"""
    if total <= 100:
        # 短棋局: 前30%序盘, 后70%中盘
        if move_num <= total * 0.3:
            return "opening"
        return "midgame"

    if move_num <= 60:
        return "opening"
    if move_num <= total * 0.85:
        return "midgame"
    return "endgame"


# ── VisitsStrategy ────────────────────────────────────

class VisitsStrategy:
    """可配置的 visits 策略"""

    def __init__(self, profile: str = "precision", **overrides):
        if profile not in PROFILES:
            raise ValueError(f"Unknown profile: {profile}, choose from {list(PROFILES.keys())}")
        self._config = PROFILES[profile].copy()
        self._config.update(overrides)
        self._profile = profile

    @classmethod
    def from_config(cls, cfg: ConfigManager) -> "VisitsStrategy":
        """从 ConfigManager 构建"""
        enabled = cfg.get("analyzer.visits_smart", True)
        if not enabled:
            return cls("precision")

        base = cfg.get("analyzer.visits", 96)
        proto = cfg.get("analyzer.visits_prototype", 25)
        batch = cfg.get("analyzer.visits_batch", 50)
        precis = cfg.get("analyzer.visits_precision", 96)

        # 根据 base 值选择 profile
        if base <= 25:
            return cls("prototype", base=base)
        elif base <= 50:
            return cls("batch", base=batch)
        else:
            return cls("precision", base=precis)

    @property
    def profile_name(self) -> str:
        return self._profile

    def get_visits(self, move_num: int, total: int,
                   prev_eval_change: Optional[float] = None) -> int:
        """返回推荐 visits 数"""
        cfg = self._config
        base = cfg["base"]

        phase = game_phase(move_num, total)

        if phase == "opening":
            visits = int(base * cfg["opening_mult"])
        elif phase == "midgame":
            if is_critical_move(move_num, total, prev_eval_change):
                visits = int(base * cfg["critical_mult"])
            else:
                visits = cfg["midgame_std"]
        else:  # endgame
            visits = cfg["endgame_std"]

        return max(visits, 1)

    def analyze_budget(self, total_moves: int) -> tuple[int, int, int]:
        """返回 (total_visits, avg_per_move, saved_vs_flat)"""
        total = 0
        for m in range(1, total_moves + 1):
            total += self.get_visits(m, total_moves)
        avg = total // total_moves
        flat_total = PROFILES[self._profile]["base"] * total_moves
        saved = flat_total - total
        return total, avg, saved

    def __repr__(self) -> str:
        return f"VisitsStrategy(profile={self._profile}, base={self._config['base']})"


# ── 独立函数 ────────────────────────────────────────

def smart_visits(move_num: int, total: int,
                 profile: str = "precision",
                 prev_eval_change: Optional[float] = None) -> int:
    """快速调用的智能 visits 函数"""
    strat = VisitsStrategy(profile)
    return strat.get_visits(move_num, total, prev_eval_change)

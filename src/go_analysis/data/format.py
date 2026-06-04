"""
AnalysisRecord — 单一 NPZ 文件的数据契约。

包含了从 SGF 分析结果到模型训练输入的全部信息。
12维 move feature 是 analyzer 和 evaluation 之间的数据契约。
"""
import json
from dataclasses import dataclass, field, asdict
from typing import Optional
import numpy as np


# ── 12维特征定义 ──────────────────────────────────────────────
FEATURE_NAMES = [
    "is_best",        # 该手是否是 KataGo 选的最佳 (order==0)
    "is_top5",        # 该手是否在 top5 候选内 (order<5)
    "1_max_policy",   # 1 - 策略网络置信度 (值越小越确信)
    "entropy",        # 策略网络熵 (局面复杂度)
    "prior",          # 策略网络先验概率
    "winrate",        # 胜率
    "score_lead",     # 领先目数
    "score_stdev",    # 目数标准差 (不确定性)
    "utility",        # KataGo 效用值
    "lcb",            # 下置信边界
    "visits_ratio",   # 该手访问次数 / 总访问次数
    "side",           # 执黑=0 / 执白=1
]

NUM_FEATURES = len(FEATURE_NAMES)  # 12


@dataclass
class HardwareEnv:
    """硬件环境采集"""
    cpu_model: str = ""
    cpu_cores: int = 0
    gpu_model: str = ""
    gpu_memory_mb: int = 0
    ram_gb: float = 0.0


@dataclass
class SoftwareEnv:
    """软件环境采集"""
    os: str = ""
    python_version: str = ""
    katago_version: str = ""
    cuda_version: str = ""
    opencl_version: str = ""


@dataclass
class GameMeta:
    """棋谱元数据"""
    game_id: str = ""
    black_player: str = ""
    white_player: str = ""
    black_rank: Optional[int] = None
    white_rank: Optional[int] = None
    komi: float = 6.5
    result: str = ""
    move_count: int = 0
    board_size: int = 19
    handicap: int = 0
    source: str = ""         # GTL / KGS / OGS / Yunyi / ...
    group: str = ""          # 1d-3d / 30k-10k / pro / ...


@dataclass
class AnalysisRecord:
    """完整分析结果记录 — 一个 NPZ 文件对应一个实例。"""
    game_id: str
    features: np.ndarray          # (N, 12) float32
    metadata: GameMeta = field(default_factory=GameMeta)
    hardware_env: HardwareEnv = field(default_factory=HardwareEnv)
    software_env: SoftwareEnv = field(default_factory=SoftwareEnv)
    visits_used: int = 25
    analysis_duration_s: float = 0.0

    def __post_init__(self):
        if isinstance(self.features, list):
            self.features = np.array(self.features, dtype=np.float32)
        if isinstance(self.metadata, dict):
            self.metadata = GameMeta(**self.metadata)
        if isinstance(self.hardware_env, dict):
            self.hardware_env = HardwareEnv(**self.hardware_env)
        if isinstance(self.software_env, dict):
            self.software_env = SoftwareEnv(**self.software_env)

    def to_npz_dict(self) -> dict:
        """序列化为 np.savez 可接受的 dict。"""
        d = {
            "features": self.features.astype(np.float32),
            "game_id": self.game_id,
            "black_player": self.metadata.black_player or "",
            "white_player": self.metadata.white_player or "",
            "black_rank": self.metadata.black_rank or -1,
            "white_rank": self.metadata.white_rank or -1,
            "komi": self.metadata.komi,
            "result": self.metadata.result or "",
            "move_count": self.metadata.move_count,
            "board_size": self.metadata.board_size,
            "handicap": self.metadata.handicap,
            "source": self.metadata.source or "",
            "group": self.metadata.group or "",
            "visits_used": self.visits_used,
            "analysis_duration_s": self.analysis_duration_s,
            "hardware_env": json.dumps(asdict(self.hardware_env)),
            "software_env": json.dumps(asdict(self.software_env)),
        }
        return d

    @classmethod
    def from_npz(cls, data: dict) -> "AnalysisRecord":
        """从 np.load() 返回的 dict 反序列化。"""
        def _val(v, default=""):
            """从 numpy 标量中提取 Python 原生值。"""
            if v is None:
                return default
            if hasattr(v, 'item'):
                return v.item()
            return v

        features = np.array(data.get("features", np.zeros((0, 12))), dtype=np.float32)
        meta = GameMeta(
            game_id=_val(data.get("game_id", "")),
            black_player=_val(data.get("black_player", "")),
            white_player=_val(data.get("white_player", "")),
            black_rank=int(_val(data.get("black_rank", -1))) if int(_val(data.get("black_rank", -1))) >= 0 else None,
            white_rank=int(_val(data.get("white_rank", -1))) if int(_val(data.get("white_rank", -1))) >= 0 else None,
            komi=float(_val(data.get("komi", 6.5))),
            result=_val(data.get("result", "")),
            move_count=int(_val(data.get("move_count", 0))),
            board_size=int(_val(data.get("board_size", 19))),
            handicap=int(_val(data.get("handicap", 0))),
            source=_val(data.get("source", "")),
            group=_val(data.get("group", "")),
        )
        hw_raw = _val(data.get("hardware_env", "{}"))
        sw_raw = _val(data.get("software_env", "{}"))
        hw = HardwareEnv(**json.loads(hw_raw))
        sw = SoftwareEnv(**json.loads(sw_raw))
        return cls(
            game_id=meta.game_id,
            features=features,
            metadata=meta,
            hardware_env=hw,
            software_env=sw,
            visits_used=int(_val(data.get("visits_used", 25))),
            analysis_duration_s=float(_val(data.get("analysis_duration_s", 0))),
        )

    @property
    def num_moves(self) -> int:
        return len(self.features)

    def __repr__(self):
        return (f"AnalysisRecord(game_id={self.game_id}, "
                f"moves={self.num_moves}, "
                f"features={self.features.shape})")

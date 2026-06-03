"""
压缩分析记录格式 (Analysis Record Format v2).

目标：将 KataGo 的原始 JSON 分析结果压缩为高效的低空间占用的结构化格式。

存储格式: .npz (Numpy 二进制压缩)
每个棋谱一个文件:
  - features:      (T, 12) float16 — 每步 12 维特征
  - global_stats:  (12,)    float16 — 整局聚合特征
  - move_count:    uint16          — 有效步数
  - env_hardware:  JSON string     — 硬件环境向量
  - env_software:  JSON string     — 软件环境向量
  - env_game:      JSON string     — 棋谱环境向量

空间估算 (200步一局):
  features:     200 × 12 × 2B  = 4.8 KB
  global_stats: 12 × 2B        = 24 B
  env_*:        3 × ~200B      = 600 B
  ─────────────────────────────────
  总计:         ~5.4 KB/局

1 万局: ~54 MB，10 万局: ~540 MB  — 远小于 raw JSON (>1GB/万局)
"""

import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import numpy as np


# ── 三大环境向量 ──────────────────────────────────────────

@dataclass
class HardwareEnv:
    """硬件环境向量 — 分析运行时的系统硬件指标."""
    cpu_model: str = ""
    cpu_cores: int = 0
    cpu_threads: int = 0
    gpu_model: str = ""          # 如 "NVIDIA GeForce GTX 1660 Ti"
    gpu_vram_gb: float = 0.0     # VRAM 大小
    ram_gb: float = 0.0          # 系统内存
    gpu_count: int = 0
    timestamp: str = ""          # 分析时间

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SoftwareEnv:
    """软件环境向量 — 分析和训练的软件栈信息."""
    cuda_version: str = ""
    torch_version: str = ""
    katago_version: str = ""
    katago_model: str = ""       # 使用的模型权重名
    katago_max_visits: int = 50
    katago_num_threads: int = 4
    python_version: str = ""
    os_info: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GameMeta:
    """棋谱环境向量 — 每局棋的基本元数据."""
    player_black: str = ""
    player_white: str = ""
    rank_black: str = ""         # 段位，如 "5d"
    rank_white: str = ""
    total_moves: int = 0         # 总步数（区分快棋/慢棋风格）
    rules: str = "chinese"
    komi: float = 7.5
    handicap: int = 0
    board_size: int = 19
    result: str = ""             # 如 "B+R", "W+2.5"
    game_date: str = ""
    event_name: str = ""
    time_settings: str = ""      # SGF 的 TM 字段，如 "600" 秒
    sgf_path: str = ""
    game_id: str = ""

    # ── 字段归一化 ──────────────────────────────────────

    _RULES_ALIAS = {
        "chinese": "chinese", "cn": "chinese", "china": "chinese",
        "japanese": "japanese", "jp": "japanese", "japan": "japanese",
        "korean": "korean", "kr": "korean", "korea": "korean",
        "aga": "aga", "us": "aga",
        "new_zealand": "new_zealand", "nz": "new_zealand",
        "ing": "ing", "ing's": "ing", "ings": "ing",
        "chinese_simple": "chinese", "chinese_traditional": "chinese",
    }

    _RANK_RE = re.compile(r"(\d+)\s*([dkp段])", re.IGNORECASE)

    @staticmethod
    def _norm_rank(raw: str) -> str:
        """归一化段位: '5段'→'5d', '9D'→'9d', 'P1'→'1p', 空→''"""
        if not raw:
            return ""
        # 职业段位: "P1段", "p1d", "1p"
        m = re.search(r"([pP])\s*(\d+)", raw)
        if m:
            return f"{m.group(2)}p"
        m = GameMeta._RANK_RE.search(raw)
        if m:
            num, suffix = m.group(1), m.group(2).lower()
            if suffix == "段":
                suffix = "d"
            return f"{num}{suffix}"
        return ""

    @staticmethod
    def _norm_date(raw: str) -> str:
        """归一化日期到 YYYY-MM-DD 格式。

        处理: '2016-03-15', '20160315', '2016.03.15', '2016-03-15a',
              '2016-03-15,2016-03-16' (取第一个), '2016-3-5'
        """
        if not raw:
            return ""
        # 取第一个日期 (有些 SGF 存为区间)
        raw = raw.split(",")[0].strip()
        # 去掉尾部非数字字符
        raw = re.sub(r"[^0-9.\-/]+.*$", "", raw)
        for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y%m%d", "%Y-%m", "%Y"):
            try:
                return datetime.strptime(raw[:10], fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

    @staticmethod
    def _norm_result(raw: str) -> str:
        """归一化对局结果。

        格式: B+R, W+R, B+T, W+T, B+N.N, W+N.N, Draw, Void, ?
        """
        if not raw:
            return "?"
        raw = raw.strip().upper()
        # "0" (某些服务器的 resign = 0)
        if raw in ("0",):
            return "B+R"  # default assumption, can be overridden
        # "B+RESIGN" → "B+R"
        raw = re.sub(r"\+RESIGN", "+R", raw)
        raw = re.sub(r"\+TIME", "+T", raw)
        raw = re.sub(r"\+FORFEIT", "+F", raw)
        if raw in ("DRAW", "DRAW?", "JIGO", "平局"):
            return "Draw"
        if raw in ("VOID", "Void", "?"):
            return "?"
        return raw

    @staticmethod
    def _norm_rules(raw: str) -> str:
        """归一化规则名到标准形式。"""
        if not raw:
            return "chinese"
        return GameMeta._RULES_ALIAS.get(raw.strip().lower(), raw.strip().lower())

    @staticmethod
    def _norm_time(raw: str) -> str:
        """归一化时间设定。

        处理: '600' (秒), '600/1/60' (加拿大小秒), '30 1' (日本式)
        返回: 以秒为单位的字符串，或空
        """
        if not raw:
            return ""
        raw = raw.strip()
        # 纯数字 = 秒
        if raw.isdigit():
            return raw
        # "600/1/60" → "600"
        parts = raw.split("/")
        if parts and parts[0].isdigit():
            return parts[0]
        return raw

    def normalize(self) -> "GameMeta":
        """原地归一化所有字段，返回 self。"""
        self.player_black = self.player_black.strip()
        self.player_white = self.player_white.strip()
        self.rank_black = self._norm_rank(self.rank_black)
        self.rank_white = self._norm_rank(self.rank_white)
        self.result = self._norm_result(self.result)
        self.rules = self._norm_rules(self.rules)
        self.game_date = self._norm_date(self.game_date)
        self.time_settings = self._norm_time(self.time_settings)
        self.handicap = int(self.handicap) if self.handicap else 0
        self.board_size = int(self.board_size) if self.board_size else 19
        self.komi = float(self.komi) if self.komi else 7.5
        return self

    def to_dict(self) -> dict:
        return asdict(self)


# ── 压缩记录 ──────────────────────────────────────────────

@dataclass
class AnalysisRecord:
    """单个棋谱的压缩分析记录."""
    features: np.ndarray          # (T, 12) float16 — 每步特征
    global_stats: np.ndarray      # (12,) float16   — 全局聚合
    move_count: int               # 有效步数
    hw: HardwareEnv = field(default_factory=HardwareEnv)
    sw: SoftwareEnv = field(default_factory=SoftwareEnv)
    game: GameMeta = field(default_factory=GameMeta)

    @classmethod
    def compress(
        cls,
        features: np.ndarray,
        global_stats: np.ndarray,
        hw: Optional[HardwareEnv] = None,
        sw: Optional[SoftwareEnv] = None,
        game: Optional[GameMeta] = None,
    ) -> "AnalysisRecord":
        """从特征矩阵和环境向量压缩为记录。

        Parameters
        ----------
        features : (T, 12) float32
            原始 float32 特征，压缩时转为 float16。
        global_stats : (12,) float32
        hw, sw, game : 环境向量，不传则留空
        """
        return cls(
            features=np.asarray(features, dtype=np.float16),
            global_stats=np.asarray(global_stats, dtype=np.float16),
            move_count=features.shape[0],
            hw=hw or HardwareEnv(),
            sw=sw or SoftwareEnv(),
            game=game or GameMeta(),
        )

    def get_features_f32(self) -> np.ndarray:
        """解压为 float32。"""
        return self.features.astype(np.float32)

    def get_global_stats_f32(self) -> np.ndarray:
        return self.global_stats.astype(np.float32)

    def to_npz(self, path: str):
        """写入 .npz 文件。"""
        np.savez_compressed(
            path,
            features=self.features,
            global_stats=self.global_stats,
            move_count=np.uint16(self.move_count),
            env_hardware=json.dumps(self.hw.to_dict()),
            env_software=json.dumps(self.sw.to_dict()),
            env_game=json.dumps(self.game.to_dict()),
        )

    @classmethod
    def from_npz(cls, path: str) -> "AnalysisRecord":
        """从 .npz 文件加载。"""
        with np.load(path, allow_pickle=True) as data:
            hw = HardwareEnv(**json.loads(data["env_hardware"].item()))
            sw = SoftwareEnv(**json.loads(data["env_software"].item()))
            game = GameMeta(**json.loads(data["env_game"].item()))
            return cls(
                features=data["features"],
                global_stats=data["global_stats"],
                move_count=int(data["move_count"]),
                hw=hw, sw=sw, game=game,
            )

    def size_bytes(self) -> int:
        """估算内存占用。"""
        return (
            self.features.nbytes
            + self.global_stats.nbytes
            + 2  # move_count
            + len(json.dumps(self.hw.to_dict()))
            + len(json.dumps(self.sw.to_dict()))
            + len(json.dumps(self.game.to_dict()))
        )

    def summary(self) -> str:
        """一行摘要。"""
        return (
            f"[Record] moves={self.move_count}, "
            f"hw={self.hw.gpu_model or 'N/A'}, "
            f"game={self.game.player_black} vs {self.game.player_white}, "
            f"size={self.size_bytes() / 1024:.1f}KB"
        )


# ── 批量存储 ──────────────────────────────────────────────

class AnalysisStore:
    """批量分析记录的管理器 (兼容原接口, 底层委托 storage 模块).

    提供目录级别的存储、检索和统计功能。
    """

    def __init__(self, store_dir: str):
        from .storage import AnalysisStore as NewStore, FileStorageBackend
        self._impl = NewStore(FileStorageBackend(store_dir))

    @property
    def store_dir(self) -> str:
        return self._impl.store_dir

    def put(self, game_id: str, record: AnalysisRecord):
        self._impl.put(game_id, record)

    def get(self, game_id: str) -> Optional[AnalysisRecord]:
        return self._impl.get(game_id)

    def list_games(self) -> list:
        return self._impl.list_games()

    def stats(self) -> dict:
        return self._impl.stats()

    def close(self):
        self._impl.close()

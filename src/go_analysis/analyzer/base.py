"""BaseAnalyzer — KataGo 分析器抽象基类。

定义统一的接口契约:
  - analyze(moves) → AnalysisResult
  - benchmark() → dict
  - tune() → dict
  - discover() → dict
"""
import abc
from dataclasses import dataclass, field
from typing import Optional
import numpy as np


@dataclass
class AnalysisResult:
    """KataGo 分析结果。

    features:  (N, 12) float32 ndarray  12维 move 特征矩阵
    move_infos: list[dict]               原始 KataGo moveInfos (调试用)
    root_info: dict                      原始 KataGo rootInfo (调试用)
    duration_s: float                    分析耗时
    success: bool                        是否成功
    """
    features: np.ndarray = field(default_factory=lambda: np.zeros((0, 12), dtype=np.float32))
    move_infos: list = field(default_factory=list)
    root_info: dict = field(default_factory=dict)
    duration_s: float = 0.0
    success: bool = False
    visits_used: int = 25

    def __post_init__(self):
        if isinstance(self.features, list):
            self.features = np.array(self.features, dtype=np.float32)

    @property
    def num_moves(self) -> int:
        return len(self.features)


def extract_12dim_features(responses: dict, moves: list) -> np.ndarray:
    """从 KataGo 分析响应中提取 12 维特征矩阵。

    Args:
        responses: {move_idx: kata_response_json}
        moves: [[player, gtp_coord], ...]  主线位移

    Returns:
        (N, 12) float32 ndarray

    12 维特征:
        0  is_best      该手被选为最佳 (order==0)
        1  is_top5      该手在 top5 候选内 (order<5)
        2  1_max_policy 1 - 策略网络置信度
        3  entropy      策略网络熵 (局面复杂度)
        4  prior        策略网络先验概率
        5  winrate      胜率
        6  score_lead   领先目数
        7  score_stdev  目数标准差
        8  utility      KataGo 效用值
        9  lcb          下置信边界
        10 visits_ratio 该手访问次数 / 总访问次数
        11 side         执黑=0 / 执白=1
    """
    n = len(moves)
    feats = np.zeros((n, 12), dtype=np.float32)

    for qi in range(n):
        resp = responses.get(qi, {})
        if not resp:
            continue
        player, coord = moves[qi]
        infos = resp.get("moveInfos", [])
        found = next((m for m in infos if m.get("move") == coord), None)

        if found:
            o = found.get("order", -1)
            p = found.get("prior", 0)
            w = found.get("winrate", 0.5)
            sl = found.get("scoreLead", 0)
            sd = found.get("scoreStdev", 0)
            u = found.get("utility", 0)
            l = found.get("lcb", 0)
            v = found.get("visits", 0)
        else:
            o = len(infos)
            p = 0
            w = 0.5
            sl = 0
            sd = 0
            u = 0
            l = 0
            v = 0

        # 策略熵
        plist = resp.get("rootInfo", {}).get("policy", [])
        if plist:
            clipped = np.clip(np.array(plist, dtype=np.float32), 1e-10, 1)
            entropy = -float(np.sum(clipped * np.log(clipped)))
        else:
            entropy = 0

        max_policy = max(plist) if plist else 1
        total_visits = sum(m.get("visits", 0) for m in infos) or 1
        visits_ratio = v / total_visits

        feats[qi] = [
            float(o == 0),
            float(o < 5),
            1.0 - max_policy,
            entropy,
            p,
            w,
            sl,
            sd,
            u,
            l,
            visits_ratio,
            float(0 if player == "B" else 1),
        ]

    return feats


class BaseAnalyzer(abc.ABC):
    """KataGo 分析器抽象基类。"""

    @abc.abstractmethod
    def analyze(self, moves: list) -> AnalysisResult:
        """分析一局棋的位移序列。

        Args:
            moves: [[player, gtp_coord], ...]  主线位移

        Returns:
            AnalysisResult 包含 12 维特征矩阵
        """
        ...

    @abc.abstractmethod
    def benchmark(self, test_moves: list, visits_range: list = None) -> dict:
        """基准测试 — 找出最优 visits 配置。

        Returns: {"best_visits": N, "vps": X, ...}
        """
        ...

    def tune(self, test_moves: list) -> dict:
        """参数调优 (默认基于 benchmark 结果)。"""
        return self.benchmark(test_moves)

    def discover(self) -> dict:
        """自动发现环境信息。返回环境检测结果 dict。"""
        import platform, sys, os, subprocess
        info = {
            "os": platform.system(),
            "os_release": platform.release(),
            "python": sys.version,
            "wsl": "microsoft" in platform.uname().release.lower() if hasattr(platform, 'uname') else False,
        }
        return info


def create_analyzer(platform_type: str = "auto", **kwargs) -> BaseAnalyzer:
    """工厂函数 — 创建合适的分析器实例。

    Args:
        platform_type: "auto" | "local" | "windows" | "ssh" | "http"
        **kwargs: 传递给具体实现类的参数

    Returns:
        BaseAnalyzer 实例
    """
    if platform_type == "auto":
        # 自动检测
        import platform as _platform
        uname = getattr(_platform, 'uname', lambda: None)()
        if uname and "microsoft" in uname.release.lower():
            platform_type = "windows"  # WSL → Windows KataGo
        else:
            platform_type = "local"

    if platform_type == "windows":
        from .windows import WindowsAnalyzer
        return WindowsAnalyzer(**kwargs)
    elif platform_type == "local":
        from .local import LocalAnalyzer
        return LocalAnalyzer(**kwargs)
    elif platform_type == "ssh":
        from .remote import SshRemoteAnalyzer
        return SshRemoteAnalyzer(**kwargs)
    elif platform_type == "http":
        from .remote import HttpRemoteAnalyzer
        return HttpRemoteAnalyzer(**kwargs)
    else:
        raise ValueError(f"Unknown analyzer platform: {platform_type}")

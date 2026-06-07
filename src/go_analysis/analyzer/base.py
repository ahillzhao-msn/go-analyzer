"""BaseAnalyzer — KataGo 分析器抽象基类。

接口契约:
  - analyze()      分析一局棋 → AnalysisResult
  - shutdown()     释放资源
  - benchmark()    最优 visits 基准测试
  - tune()         自动参数调优（线程/批次/访问数）
  - discover()     环境自动发现

tune() 是全自动入口，调用 tuning.tune_config() 做 GPU 参数搜索，
再对最佳配置做 visits 扫描。
"""
import abc
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
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


def _default_bench_moves() -> list:
    """50 手标准测试棋谱。"""
    return [
        {"x": 3, "y": 3}, {"x": 15, "y": 15}, {"x": 3, "y": 15}, {"x": 15, "y": 3},
        {"x": 3, "y": 9}, {"x": 15, "y": 9}, {"x": 9, "y": 3}, {"x": 9, "y": 15},
        {"x": 6, "y": 5}, {"x": 12, "y": 13}, {"x": 5, "y": 12}, {"x": 13, "y": 6},
        {"x": 7, "y": 2}, {"x": 11, "y": 16}, {"x": 2, "y": 11}, {"x": 16, "y": 7},
        {"x": 10, "y": 5}, {"x": 8, "y": 13}, {"x": 4, "y": 7}, {"x": 14, "y": 11},
        {"x": 8, "y": 2}, {"x": 10, "y": 16}, {"x": 2, "y": 8}, {"x": 16, "y": 10},
        {"x": 9, "y": 7}, {"x": 9, "y": 11}, {"x": 7, "y": 9}, {"x": 11, "y": 9},
        {"x": 5, "y": 5}, {"x": 13, "y": 13}, {"x": 5, "y": 13}, {"x": 13, "y": 5},
        {"x": 10, "y": 3}, {"x": 8, "y": 15}, {"x": 3, "y": 10}, {"x": 15, "y": 8},
        {"x": 6, "y": 9}, {"x": 12, "y": 9}, {"x": 9, "y": 6}, {"x": 9, "y": 12},
        {"x": 7, "y": 6}, {"x": 11, "y": 12}, {"x": 6, "y": 11}, {"x": 12, "y": 7},
        {"x": 4, "y": 4}, {"x": 14, "y": 14}, {"x": 4, "y": 14}, {"x": 14, "y": 4},
        {"x": 9, "y": 5}, {"x": 9, "y": 13},
    ]


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

    def shutdown(self):
        """释放资源（如关闭常驻进程）。子类按需重写。"""
        pass

    # ── 核心属性（子类应设置）──

    @property
    def katago_path(self) -> str:
        raise NotImplementedError

    @property
    def model_path(self) -> str:
        raise NotImplementedError

    @property
    def config_path(self) -> Optional[str]:
        return None

    # ── 基准测试 ────────────────────────────────────

    def benchmark(self, test_moves: list = None,
                  visits_range: list = None) -> dict:
        """基准测试 — 找出最优 visits 配置。

        在当前的 config 下测试不同 visits 值的 VPS 表现。

        Args:
            test_moves: 测试棋谱位移（默认 50 手标准开局）
            visits_range: 要测试的 visits 值列表

        Returns:
            {"best_visits": N, "best_vps": X, "results": [{visits, duration_s, vps}, ...]}
        """
        if test_moves is None:
            test_moves = _default_bench_moves()
        if visits_range is None:
            visits_range = [25, 50, 100, 200]

        original = getattr(self, "visits", 25)
        results = []

        for v in visits_range:
            self.visits = v
            t0 = time.time()
            result = self.analyze(test_moves[:min(50, len(test_moves))])
            dt = time.time() - t0
            vps = v * result.num_moves / max(dt, 0.1) if result.success else 0
            results.append({
                "visits": v,
                "duration_s": round(dt, 2),
                "vps": round(vps, 1),
                "moves": result.num_moves,
                "success": result.success,
            })

        self.visits = original

        best = max(results, key=lambda r: r.get("vps", 0)) if results else {}
        return {
            "best_visits": best.get("visits", original),
            "best_vps": best.get("vps", 0),
            "results": results,
        }

    # ── 自动调优（核心）──────────────────────────────

    def tune(self, output_config_path: Optional[str] = None) -> dict:
        """全自动生产环境调优。

        流程:
          1. discover() → 检测环境（GPU、CPU、OS）
          2. tune_config() → GPU 参数搜索（线程/批次）
          3. benchmark() → 在最佳配置下找最优 visits
          4. 返回综合报告 + 可选写推荐 config

        Returns:
            {
                "environment": {...},
                "config_tune": {
                    "best_config": {numSearchThreads, numAnalysisThreads, nnMaxBatchSize, vps},
                    "results": [...],
                    "diagnosis": "最佳"|"保守"|"不稳定",
                },
                "benchmark": {
                    "best_visits": N,
                    "results": [...],
                },
                "recommended_config_path": str or "",
            }
        """
        from .tuning import tune_config as _tune_config

        env = self.discover()

        config_tune = _tune_config(
            katago_path=self.katago_path,
            model_path=self.model_path,
            base_config_path=self.config_path,
            output_path=output_config_path,
        )

        # 如果调优成功，更新自身配置
        benchmark_result = {}
        best_cfg = config_tune.get("best_config", {})
        if best_cfg.get("vps", 0) > 0:
            self.numSearchThreads = best_cfg.get("numSearchThreads",
                                                  getattr(self, "numSearchThreads", 8))
            self.numAnalysisThreads = best_cfg.get("numAnalysisThreads",
                                                    getattr(self, "numAnalysisThreads", 4))
            self.nnMaxBatchSize = best_cfg.get("nnMaxBatchSize",
                                               getattr(self, "nnMaxBatchSize", 50))

            # 如果 tune_config 写入了推荐配置文件，用它；否则写临时 cfg 跑 benchmark
            rec_path = config_tune.get("recommended_config_path", "")
            if rec_path and Path(rec_path).exists():
                self.config_path = rec_path
            else:
                tmp_cfg = Path(tempfile.mktemp(suffix=".cfg"))
                try:
                    tmp_cfg.write_text(
                        f"numSearchThreads = {self.numSearchThreads}\n"
                        f"nnMaxBatchSize = {self.nnMaxBatchSize}\n"
                        f"numAnalysisThreads = {self.numAnalysisThreads}\n"
                    )
                    self.config_path = str(tmp_cfg)
                    benchmark_result = self.benchmark()
                finally:
                    tmp_cfg.unlink(missing_ok=True)

            # 杀当前进程，下次 analyze 用新配置启动
            self.shutdown()

        return {
            "environment": env,
            "config_tune": config_tune,
            "benchmark": benchmark_result,
            "recommended_config_path": config_tune.get("recommended_config_path", ""),
        }

    # ── 环境发现 ────────────────────────────────────

    def discover(self) -> dict:
        """自动发现环境信息。子类可扩展。"""
        from .tuning import guess_vram_mb
        import platform, sys

        info = {
            "os": platform.system(),
            "os_release": platform.release(),
            "python": sys.version,
            "cpu_cores": os.cpu_count() or 8,
            "vram_mb": guess_vram_mb(),
        }

        # WSL 检测
        uname = getattr(platform, 'uname', lambda: None)()
        if uname and "microsoft" in uname.release.lower():
            info["wsl"] = True
            info["os"] = "WSL"

        # GPU 厂商
        try:
            import subprocess as _sp
            r = _sp.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                info["gpu_name"] = r.stdout.strip()
        except Exception:
            pass

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

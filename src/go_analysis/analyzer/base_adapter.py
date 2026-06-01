"""
KataGo 适配器抽象基类。

所有平台实现（Windows原生、WSL-CUDA、SSH远程等）必须继承此类。
"""

import abc
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AnalysisResult:
    """单局分析结果的标准容器。"""
    game_id: str = ""
    success: bool = False
    raw_json: Optional[dict] = None
    error: str = ""
    duration_s: float = 0.0
    visits_used: int = 0
    move_count: int = 0


class BaseAdapter(abc.ABC):
    """抽象基类 — 所有 KataGo 适配器必须实现的方法。"""

    # ── 生命周期 ────────────────────────────────────────

    @abc.abstractmethod
    def start(self):
        """启动适配器（初始化子进程、连接等）。"""
        ...

    @abc.abstractmethod
    def shutdown(self):
        """关闭适配器，释放资源。"""
        ...

    @abc.abstractmethod
    def is_healthy(self) -> bool:
        """检查适配器是否健康可用。"""
        ...

    # ── 核心方法 ────────────────────────────────────────

    @abc.abstractmethod
    def analyze(self, sgf_content: str, game_id: str = "",
                visits: int = 50, **kwargs) -> AnalysisResult:
        """分析一局棋谱。

        Parameters
        ----------
        sgf_content : str
            SGF 内容字符串。
        game_id : str
            游戏标识符。
        visits : int
            每手访问次数。

        Returns
        -------
        AnalysisResult
        """
        ...

    # ── 批量 ────────────────────────────────────────────

    def analyze_batch(self, sgf_list: list, visits: int = 50,
                      batch_size: int = 1) -> list:
        """批量分析（默认串行，子类可重写为并行）。"""
        results = []
        for sgf_content, game_id in sgf_list:
            result = self.analyze(sgf_content, game_id, visits)
            results.append(result)
        return results

    # ── 信息 ────────────────────────────────────────────

    @abc.abstractmethod
    def info(self) -> dict:
        """返回适配器信息: 平台, 版本, 模型, 配置等。"""
        ...

    @property
    @abc.abstractmethod
    def platform(self) -> str:
        """适配器平台标识。"""
        ...


def auto_detect_adapter() -> str:
    """自动检测当前平台的最佳适配器。

    Returns
    -------
    str
        适配器类名: "windows_native", "wsl_cuda", "wsl_opencl"
    """
    import sys
    import os

    # WSL + Windows .exe 可用
    if "microsoft" in os.uname().release.lower() or "wsl" in os.uname().release.lower():
        # 检查 Windows kataGo.exe 是否存在
        win_katago = os.path.expandvars(
            "katago"  # fallback: use PATH
        )
        if os.path.exists(win_katago):
            return "windows_native"

    # Linux native
    try:
        import subprocess
        r = subprocess.run(["which", "katago"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            # 检查 OpenCL 可用性
            if os.path.exists("/etc/OpenCL/vendors/nvidia.icd"):
                return "wsl_opencl"
            return "wsl_cuda"
    except Exception:
        pass

    return "unknown"


def create_adapter(platform: str = None,
                   kata_path: str = None,
                   model_path: str = None,
                   config_path: str = None,
                   **kwargs) -> "BaseAdapter":
    """工厂函数 — 创建一个适配器实例。

    Parameters
    ----------
    platform : str or None
        指定平台。None 则自动检测。
    kata_path : str or None
        KataGo 可执行文件路径。
    model_path : str or None
        模型权重路径。
    config_path : str or None
        配置文件路径。

    Returns
    -------
    BaseAdapter
    """
    if platform is None:
        platform = auto_detect_adapter()
        print(f"[Adapter] Auto-detected: {platform}")

    if platform == "windows_native":
        from .adapters.windows_native import WindowsNativeAdapter
        return WindowsNativeAdapter(kata_path=kata_path, model_path=model_path,
                                     config_path=config_path, **kwargs)
    elif platform == "wsl_opencl":
        from .adapters.wsl_opencl import WSLOpenCLAdapter
        return WSLOpenCLAdapter(kata_path=kata_path, model_path=model_path,
                                 config_path=config_path, **kwargs)
    elif platform == "wsl_cuda":
        from .adapters.wsl_cuda import WSLCUDAAdapter
        return WSLCUDAAdapter(kata_path=kata_path, model_path=model_path,
                               config_path=config_path, **kwargs)
    else:
        raise ValueError(f"Unknown adapter platform: {platform}")

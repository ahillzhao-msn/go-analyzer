"""KataGo Process — 平台无关的子进程包装器。
"""
import abc
import logging
import queue
import select
import subprocess
import sys
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)


class KataGoProcess(abc.ABC):
    """KataGo 进程抽象。封装子进程的生命周期和 I/O。"""

    @abc.abstractmethod
    def start(self, katago_path: str, model_path: str,
              config_path: Optional[str] = None) -> None:
        """启动 KataGo analysis 进程。"""
        ...

    @abc.abstractmethod
    def kill(self) -> None:
        """终止进程，释放资源。多次调用安全。"""
        ...

    @abc.abstractmethod
    def is_alive(self) -> bool:
        """进程是否仍在运行。"""
        ...

    @abc.abstractmethod
    def readline(self, deadline: float) -> Optional[str]:
        """读一行输出，超时返回 None，EOF 返回 ''。"""
        ...

    @abc.abstractmethod
    def send(self, data: str) -> None:
        """向进程 stdin 发送数据。"""
        ...

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.kill()


# ── Linux 原生实现 ──────────────────────────────────

class DirectProcess(KataGoProcess):
    """直接 subprocess.Popen（Linux/macOS 原生）。"""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None

    def start(self, katago_path: str, model_path: str,
              config_path: Optional[str] = None) -> None:
        cmd = [katago_path, "analysis", "-model", model_path]
        if config_path:
            cmd += ["-config", config_path]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )

    def kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            proc.terminate()
            proc.wait(3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def send(self, data: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise BrokenPipeError("Process not running")
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def readline(self, deadline: float) -> Optional[str]:
        """Linux：select 实现超时。"""
        if self._proc is None or self._proc.stdout is None:
            return ''
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        try:
            r, _, _ = select.select([self._proc.stdout], [], [],
                                     min(remaining, 1.0))
            if r:
                return self._proc.stdout.readline()
            return None
        except (TypeError, ValueError, OSError):
            return self._proc.stdout.readline()


# ── Windows 原生实现 ────────────────────────────────

class WindowsProcess(KataGoProcess):
    """Windows 原生 subprocess（带 CREATE_NO_WINDOW + 队列读取线程）。"""

    CREATE_NO_WINDOW = 0x08000000

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._line_queue: Optional[queue.Queue] = None
        self._reader_stop = threading.Event()

    def _reader_loop(self, stream):
        """后台线程：持续从 stdout 读行，放入队列。"""
        try:
            while not self._reader_stop.is_set():
                line = stream.readline()
                if not line:
                    # EOF — 进程结束
                    self._line_queue.put(("", None))
                    return
                self._line_queue.put(("line", line))
        except Exception as e:
            self._line_queue.put(("error", e))

    def start(self, katago_path: str, model_path: str,
              config_path: Optional[str] = None) -> None:
        cmd = [katago_path, "analysis", "-model", model_path]
        if config_path:
            cmd += ["-config", config_path]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            creationflags=self.CREATE_NO_WINDOW,
        )
        # 启动读取队列
        self._line_queue = queue.Queue()
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(self._proc.stdout,),
            daemon=True,
        )
        self._reader_thread.start()

    def kill(self) -> None:
        self._reader_stop.set()  # 通知读取线程停止
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            proc.terminate()
            proc.wait(3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # 等读取线程感知进程结束
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2)
        self._reader_thread = None
        self._line_queue = None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def send(self, data: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise BrokenPipeError("Process not running")
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def readline(self, deadline: float) -> Optional[str]:
        """Windows：从队列取出。超时返回 None，EOF 返回 ''。"""
        if self._line_queue is None:
            return ''
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        try:
            kind, data = self._line_queue.get(timeout=min(remaining, 5.0))
            if kind == "line":
                return data
            elif kind == "error":
                return ''
            elif kind == "":
                return ''
            return data
        except queue.Empty:
            return None


# ── 工厂 ────────────────────────────────────────────

def _readline_with_timeout(proc: KataGoProcess, deadline: float) -> Optional[str]:
    """从 KataGo 进程读一行，带超时。

    委托给 KataGoProcess.readline()，但处理常见异常。
    超时返回 None，EOF 返回 ''，数据返回行内容。
    """
    try:
        return proc.readline(deadline)
    except Exception:
        return None


def create_process() -> KataGoProcess:
    """根据当前平台创建合适的 KataGo 进程。"""
    if sys.platform == "win32":
        return WindowsProcess()
    # WSL 检测
    try:
        r = subprocess.run(["uname", "-r"], capture_output=True,
                           text=True, timeout=3)
        if "microsoft" in r.stdout.lower() or "wsl" in r.stdout.lower():
            # WSL 下如果 katago_path 以 .exe 结尾，需要 WindowsProcess
            # 但路径可能是 Linux 路径 → 用 cmd.exe /c
            # 调用方负责传正确路径，这里返回 DirectProcess
            pass
    except Exception:
        pass
    return DirectProcess()

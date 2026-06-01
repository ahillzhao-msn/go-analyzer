"""
分析任务 — 状态机, 优先级, 生命周期。

每个 AnalysisTask 代表一局棋谱从等待到完成（或失败/暂停）的完整生命周期。
支持: 优先级调度, 暂停/恢复, 中断续传。
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Optional


class TaskPriority(IntEnum):
    """任务优先级（数字越大优先级越高）。"""
    LOW = 0
    NORMAL = 5
    HIGH = 10
    CRITICAL = 20


class TaskState:
    """任务状态常量。"""
    PENDING = "pending"        # 等待调度
    RUNNING = "running"        # 正在分析
    PAUSED = "paused"          # 已暂停（可恢复）
    COMPLETED = "completed"    # 成功完成
    FAILED = "failed"          # 失败（可重试）
    CANCELLED = "cancelled"    # 已取消

    _TERMINAL = {COMPLETED, FAILED, CANCELLED}
    _PAUSABLE = {PENDING, RUNNING}

    @classmethod
    def is_terminal(cls, state: str) -> bool:
        return state in cls._TERMINAL

    @classmethod
    def can_pause(cls, state: str) -> bool:
        return state in cls._PAUSABLE

    @classmethod
    def can_resume(cls, state: str) -> bool:
        return state == cls.PAUSED


@dataclass
class AnalysisTask:
    """一局棋谱的分析任务。

    Parameters
    ----------
    sgf_path : str
        SGF 文件路径。
    game_id : str
        游戏标识符，用于去重和结果存储。
    visits : int
        每手访问次数。
    priority : TaskPriority
        任务优先级。
    sgf_content : str or None
        SGF 内容（如果已在内存中）。
    """

    sgf_path: str
    game_id: str = ""
    visits: int = 50
    priority: TaskPriority = TaskPriority.NORMAL
    sgf_content: Optional[str] = None

    # 内部状态
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = TaskState.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    retry_count: int = 0
    max_retries: int = 3
    error: str = ""
    result_path: str = ""       # 输出 .npz 路径

    def start(self):
        """标记任务开始。"""
        self.state = TaskState.RUNNING
        self.started_at = time.time()

    def complete(self, result_path: str = ""):
        """标记任务完成。"""
        self.state = TaskState.COMPLETED
        self.completed_at = time.time()
        self.result_path = result_path

    def fail(self, error: str = ""):
        """标记任务失败，如果还有重试次数则转为 PENDING。"""
        self.error = error
        if self.retry_count < self.max_retries:
            self.retry_count += 1
            self.state = TaskState.PENDING
            self.started_at = None
        else:
            self.state = TaskState.FAILED
            self.completed_at = time.time()

    def pause(self) -> bool:
        """暂停任务（仅 PENDING 或 RUNNING 可暂停）。"""
        if TaskState.can_pause(self.state):
            self.state = TaskState.PAUSED
            return True
        return False

    def resume(self) -> bool:
        """恢复暂停的任务。"""
        if TaskState.can_resume(self.state):
            self.state = TaskState.PENDING
            return True
        return False

    def cancel(self):
        """取消任务。"""
        self.state = TaskState.CANCELLED
        self.completed_at = time.time()

    def duration(self) -> float:
        """任务持续时间（秒）。"""
        if self.completed_at and self.started_at:
            return self.completed_at - self.started_at
        if self.started_at:
            return time.time() - self.started_at
        return 0.0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "game_id": self.game_id,
            "sgf_path": self.sgf_path,
            "state": self.state,
            "priority": self.priority.value,
            "visits": self.visits,
            "retry_count": self.retry_count,
            "duration_s": round(self.duration(), 2),
            "error": self.error,
        }

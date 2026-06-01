"""
分析池 — 多适配器管理与任务调度。

AnalysisPool 管理一组 KataGo 适配器实例，负责任务分配、负载均衡、
优先级调度、暂停/恢复、状态监控。

特性:
- 多适配器并行分析
- 优先级队列
- 自动重试失败任务
- 中断续传（基于 AnalysisStore）
- 性能监控回调
"""

import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from queue import PriorityQueue
from typing import Callable, Optional

from .base_adapter import BaseAdapter, AnalysisResult, create_adapter
from .task import AnalysisTask, TaskPriority, TaskState
from ..analysis_format import AnalysisStore


@dataclass
class PoolConfig:
    """分析池配置。"""
    max_workers: int = 1               # 最大并发适配器数
    default_visits: int = 96           # 默认访问次数
    retry_max: int = 3                 # 最大重试次数
    health_check_interval: float = 30  # 健康检查间隔（秒）
    store_dir: str = ""                # 分析结果存储目录
    auto_resume: bool = True           # 启动时恢复未完成的任务
    adapter_platform: str = "windows_native"  # 适配器平台


class AnalysisPool:
    """分析池 — 管理适配器实例和任务队列。

    Parameters
    ----------
    config : PoolConfig
    adapter_factory : Callable or None
        创建适配器的工厂函数。默认使用 create_adapter。
    """

    def __init__(self, config: PoolConfig = None,
                 adapter_factory: Callable = None):
        self.config = config or PoolConfig()
        self._adapter_factory = adapter_factory

        # 适配器实例池
        self._adapters: list[BaseAdapter] = []
        self._adapter_busy: dict[int, bool] = {}  # id → is_busy

        # 任务队列 (优先级队列: (priority, created_at, task))
        self._queue: PriorityQueue = PriorityQueue()
        self._tasks: dict[str, AnalysisTask] = {}  # task_id → task
        self._running_tasks: dict[str, int] = {}    # task_id → adapter_index

        # 统计
        self._stats = defaultdict(int)

        # 监控线程
        self._running = False
        self._lock = threading.Lock()
        self._scheduler_thread: Optional[threading.Thread] = None

        # 回调
        self.on_task_complete: Optional[Callable] = None
        self.on_task_failed: Optional[Callable] = None
        self.on_progress: Optional[Callable] = None

    # ── 生命周期 ────────────────────────────────────────

    def start(self):
        """启动分析池。"""
        if self._running:
            return

        self._running = True

        # 初始化适配器
        for i in range(self.config.max_workers):
            adapter = self._create_adapter()
            adapter.start()
            self._adapters.append(adapter)
            self._adapter_busy[id(adapter)] = False

        # 启动调度线程 (每个 worker 一个独立线程)
        self._scheduler_threads = []
        for i in range(self.config.max_workers):
            t = threading.Thread(
                target=self._worker_loop, daemon=True,
                name=f"pool-worker-{i}", args=(i,)
            )
            t.start()
            self._scheduler_threads.append(t)

        print(f"[Pool] Started with {self.config.max_workers} workers")

    def register_adapter(self, adapter: "BaseAdapter"):
        """注册一个外部适配器实例 (SSH 远程等)。"""
        adapter.start()
        self._adapters.append(adapter)
        self._adapter_busy[id(adapter)] = False
        worker_id = len(self._adapters) - 1
        t = threading.Thread(
            target=self._worker_loop, daemon=True,
            name=f"pool-worker-{worker_id}", args=(worker_id,)
        )
        t.start()
        self._scheduler_threads.append(t)
        print(f"[Pool] Registered: {adapter.platform}")
        return self

    def _worker_loop(self, worker_id: int):
        """每个 worker 的独立调度循环。"""
        adapter = self._adapters[worker_id]
        adapter_id = id(adapter)

        while self._running:
            # 取下一个任务
            task = self._get_next_task()
            if task is None:
                time.sleep(0.5)
                continue

            # 执行
            self._execute_task(adapter, task)

    def _create_adapter(self) -> BaseAdapter:
        """创建适配器实例。"""
        if self._adapter_factory:
            adapter = self._adapter_factory()
            adapter.start()
            return adapter
        adapter = create_adapter(platform=self.config.adapter_platform)
        adapter.start()
        return adapter

    def submit_from_directory(self, sgf_dir: str, visits: int = None,
                              limit: int = 0) -> int:
        """从 SGF 目录批量加载任务。

        Parameters
        ----------
        sgf_dir : str
            SGF 文件目录。
        visits : int or None
            访问次数，默认使用 config.default_visits。
        limit : int
            最大任务数，0=全部。

        Returns
        -------
        int
            提交的任务数。
        """
        import glob
        sgf_files = sorted(glob.glob(os.path.join(sgf_dir, "*.sgf")))
        if limit > 0:
            sgf_files = sgf_files[:limit]

        visits = visits or self.config.default_visits
        for path in sgf_files:
            game_id = os.path.splitext(os.path.basename(path))[0]
            task = AnalysisTask(
                sgf_path=path,
                game_id=game_id,
                visits=visits,
                priority=TaskPriority.NORMAL,
            )
            self.submit(task)

        print(f"[Pool] Submitted {len(sgf_files)} tasks from {sgf_dir}")
        return len(sgf_files)

    def shutdown(self, wait: bool = True):
        """关闭分析池。"""
        self._running = False
        for t in getattr(self, '_scheduler_threads', []):
            t.join(timeout=10)

        for adapter in self._adapters:
            try:
                adapter.shutdown()
            except Exception:
                pass

        print(f"[Pool] Shutdown. Stats: {dict(self._stats)}")

    # ── 任务管理 ────────────────────────────────────────

    def submit(self, task: AnalysisTask):
        """提交一个分析任务。"""
        with self._lock:
            self._tasks[task.task_id] = task
            # PriorityQueue: (negative priority for max-heap, timestamp)
            self._queue.put((-task.priority.value, task.created_at, task))
            self._stats["submitted"] += 1

    def submit_batch(self, tasks: list[AnalysisTask]):
        """批量提交任务。"""
        for task in tasks:
            self.submit(task)

    def pause(self, task_id: str = None):
        """暂停任务（或全部）。"""
        with self._lock:
            if task_id:
                task = self._tasks.get(task_id)
                if task:
                    task.pause()
            else:
                for t in self._tasks.values():
                    t.pause()

    def resume(self, task_id: str = None):
        """恢复暂停的任务。"""
        with self._lock:
            if task_id:
                task = self._tasks.get(task_id)
                if task and task.resume():
                    self._queue.put((-task.priority.value, time.time(), task))
            else:
                for t in self._tasks.values():
                    if t.resume():
                        self._queue.put((-t.priority.value, time.time(), t))

    def cancel(self, task_id: str = None):
        """取消任务。"""
        with self._lock:
            if task_id:
                task = self._tasks.get(task_id)
                if task:
                    task.cancel()
            else:
                for t in self._tasks.values():
                    t.cancel()

    def status(self) -> dict:
        """池状态概览。"""
        with self._lock:
            states = defaultdict(int)
            for t in self._tasks.values():
                states[t.state] += 1
            return {
                "tasks": len(self._tasks),
                "queued": states.get(TaskState.PENDING, 0),
                "running": states.get(TaskState.RUNNING, 0),
                "completed": states.get(TaskState.COMPLETED, 0),
                "failed": states.get(TaskState.FAILED, 0),
                "paused": states.get(TaskState.PAUSED, 0),
                "workers": {
                    "total": len(self._adapters),
                    "busy": sum(self._adapter_busy.values()),
                },
                "stats": dict(self._stats),
            }

    # ── 调度循环 ────────────────────────────────────────

    def _scheduler_loop(self):
        """调度器主循环。"""
        last_health_check = time.time()

        while self._running:
            now = time.time()

            # 健康检查
            if now - last_health_check > self.config.health_check_interval:
                self._check_adapters()
                last_health_check = now

            # 查找空闲适配器
            free_adapter = self._find_free_adapter()
            if free_adapter is None:
                time.sleep(0.1)
                continue

            # 获取下一个任务
            task = self._get_next_task()
            if task is None:
                time.sleep(0.5)
                continue

            # 执行
            self._execute_task(free_adapter, task)

    def _find_free_adapter(self) -> Optional[BaseAdapter]:
        """找一个空闲的适配器。"""
        for i, adapter in enumerate(self._adapters):
            if not self._adapter_busy.get(id(adapter), False) and adapter.is_healthy():
                return adapter
        return None

    def _get_next_task(self) -> Optional[AnalysisTask]:
        """从队列取下一个可执行的任务。"""
        try:
            _, _, task = self._queue.get_nowait()
            if task.state not in (TaskState.PENDING, TaskState.RUNNING):
                return self._get_next_task()  # skip non-pending
            return task
        except Exception:
            return None

    def _execute_task(self, adapter: BaseAdapter, task: AnalysisTask):
        """在指定适配器上执行任务。"""
        adapter_id = id(adapter)
        self._adapter_busy[adapter_id] = True
        task.start()

        with self._lock:
            self._running_tasks[task.task_id] = adapter_id

        # 读取 SGF 内容
        if not task.sgf_content and os.path.exists(task.sgf_path):
            try:
                with open(task.sgf_path, "r", encoding="utf-8") as f:
                    task.sgf_content = f.read()
            except Exception as e:
                task.fail(str(e))
                self._adapter_busy[adapter_id] = False
                return

        # 执行分析
        try:
            result = adapter.analyze(
                task.sgf_content, task.game_id,
                visits=task.visits,
            )
            if result.success:
                task.complete()

                # 保存到 AnalysisStore
                if self.config.store_dir and result.raw_json:
                    self._save_to_store(task, result)

                self._stats["completed"] += 1
                if self.on_task_complete:
                    self.on_task_complete(task, result)
            else:
                task.fail(result.error)
                self._stats["failed"] += 1
                if self.on_task_failed:
                    self.on_task_failed(task, result)

        except Exception as e:
            task.fail(str(e))
            self._stats["failed"] += 1

        # 清理
        self._adapter_busy[adapter_id] = False
        with self._lock:
            self._running_tasks.pop(task.task_id, None)

        self._stats["processed"] += 1
        if self.on_progress:
            self.on_progress(self.status())

        # 如果是 PENDING（重试中），重新入队
        if task.state == TaskState.PENDING:
            self._queue.put((-task.priority.value, time.time(), task))

    def _save_to_store(self, task: AnalysisTask, result: AnalysisResult):
        """保存分析结果到 AnalysisStore。"""
        try:
            from ..analysis_format import AnalysisRecord, GameMeta
            from ..env_collector import extract_game_meta_from_sgf
            from ..models import compute_global_stats
            import numpy as np

            store = AnalysisStore(self.config.store_dir)

            # 提取特征
            features_list = result.raw_json.get("features_list", [])
            if not features_list:
                return

            feats = np.stack([f["features"] for f in features_list], axis=0)
            gs = compute_global_stats(
                [type("o", (object,), {"features": f["features"]})()
                 for f in features_list]
            )

            # 提取棋谱元数据
            game = extract_game_meta_from_sgf(
                task.sgf_content or "",
                sgf_path=task.sgf_path,
                game_id=task.game_id,
            )

            # 创建记录
            record = AnalysisRecord.compress(feats, gs, game=game)
            store.put(task.game_id, record)

        except Exception as e:
            print(f"  [WARN] Save failed for {task.game_id}: {e}")

    def _check_adapters(self):
        """健康检查：重启不健康的适配器。"""
        for i, adapter in enumerate(self._adapters):
            if not adapter.is_healthy():
                print(f"[Pool] Restarting unhealthy adapter {i}")
                try:
                    adapter.shutdown()
                except Exception:
                    pass
                try:
                    new_adapter = self._create_adapter()
                    new_adapter.start()
                    self._adapters[i] = new_adapter
                    self._adapter_busy[id(new_adapter)] = False
                except Exception as e:
                    print(f"[Pool] Failed to restart adapter {i}: {e}")

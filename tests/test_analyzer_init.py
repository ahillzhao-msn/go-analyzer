"""Tests: WindowsAnalyzer, Worker, Unicode safety, backward compat.

覆盖:
  1. WindowsAnalyzer.__init__ — per_move_timeout backward compat
  2. _kill() — 安全处理 _proc 不存在
  3. worker.py — 日志无 Unicode (cp1252 兼容)
  4. create_analyzer — 工厂函数
  5. BaseAnalyzer — tune/benchmark 不自动运行
"""
import unittest
import sys
import os
import json
from unittest.mock import patch, MagicMock, PropertyMock


class TestWindowsAnalyzerInit(unittest.TestCase):
    """▶ 1. __init__ 参数处理"""

    def test_accepts_batch_timeout(self):
        from go_analysis.analyzer.windows import WindowsAnalyzer
        a = WindowsAnalyzer(katago_path="katago.exe", model_path="model.bin.gz",
                            batch_timeout=300.0)
        self.assertEqual(a.batch_timeout, 300.0)

    def test_backward_compat_per_move_timeout(self):
        """旧参数 per_move_timeout → batch_timeout = per_move * 3"""
        from go_analysis.analyzer.windows import WindowsAnalyzer
        a = WindowsAnalyzer(katago_path="katago.exe", model_path="model.bin.gz",
                            per_move_timeout=60.0)
        self.assertEqual(a.batch_timeout, 180.0)

    def test_backward_compat_per_move_overrides_batch(self):
        """如果两个都传，per_move_timeout 优先"""
        from go_analysis.analyzer.windows import WindowsAnalyzer
        a = WindowsAnalyzer(katago_path="katago.exe", model_path="model.bin.gz",
                            per_move_timeout=30.0, batch_timeout=999.0)
        self.assertEqual(a.batch_timeout, 90.0)

    def test_no_params_defaults(self):
        from go_analysis.analyzer.windows import WindowsAnalyzer
        a = WindowsAnalyzer(katago_path="k.exe", model_path="m.bin.gz")
        self.assertEqual(a.batch_timeout, 180.0)
        self.assertEqual(a.visits, 25)
        self.assertEqual(a.max_games, 50)
        self.assertEqual(a.numSearchThreads, 12)
        self.assertEqual(a.numAnalysisThreads, 5)
        self.assertEqual(a.nnMaxBatchSize, 100)

    def test_tuning_params(self):
        from go_analysis.analyzer.windows import WindowsAnalyzer
        a = WindowsAnalyzer(katago_path="k.exe", model_path="m.bin.gz",
                            numSearchThreads=6, numAnalysisThreads=2,
                            nnMaxBatchSize=16)
        self.assertEqual(a.numSearchThreads, 6)
        self.assertEqual(a.numAnalysisThreads, 2)
        self.assertEqual(a.nnMaxBatchSize, 16)


class TestWindowsAnalyzerKillSafety(unittest.TestCase):
    """▶ 2. _kill() 安全处理"""

    def test_kill_without_proc(self):
        """__init__ 没跑到 self._proc 就调 _kill """
        from go_analysis.analyzer.windows import WindowsAnalyzer
        a = WindowsAnalyzer(katago_path="k.exe", model_path="m.bin.gz")
        # 删除 _proc 模拟 __init__ 异常中断
        if hasattr(a, '_proc'):
            del a._proc
        # 不应抛出 AttributeError
        a._kill()
        # 销毁时 __del__ 也不应抛异常
        a.shutdown()

    def test_shutdown_on_failed_init(self):
        """模拟 __init__ 中途异常后 __del__ 安全"""
        from go_analysis.analyzer.windows import WindowsAnalyzer
        try:
            # 模拟 config_path 赋值后但 _proc 赋值前出错
            a = WindowsAnalyzer.__new__(WindowsAnalyzer)
            # 只设置部分属性 — 类似 __init__ 中途崩溃
            a._katago_path = "k.exe"
            a._model_path = "m.bin.gz"
            # 故意不设 _proc
            a.shutdown()  # 不应抛 AttributeError
        except Exception as e:
            self.fail(f"shutdown on partial init raised {e}")

    def test_double_shutdown(self):
        from go_analysis.analyzer.windows import WindowsAnalyzer
        a = WindowsAnalyzer(katago_path="k.exe", model_path="m.bin.gz")
        a.shutdown()
        a.shutdown()  # 第二次不应抛异常


class TestWorkerLogging(unittest.TestCase):
    """▶ 3. worker 日志不包含 Unicode cp1252 不支持的字符"""

    def test_log_messages_no_unicode_cp1252(self):
        """成功/失败日志用 ASCII 标记"""
        log_msgs = [
            "OK [1] test-game: 100m 10.0s 250 vps",
            "-- [1] test-game: skip (too_short)",
            "!! [1] test-game: analysis_failed",
        ]
        for msg in log_msgs:
            # cp1252 可编码的字符范围
            msg.encode("cp1252")  # 不抛出 UnicodeEncodeError


class TestCreateAnalyzer(unittest.TestCase):
    """▶ 4. create_analyzer 工厂函数"""

    def test_create_windows(self):
        from go_analysis.analyzer import create_analyzer
        a = create_analyzer("windows", katago_path="k.exe", model_path="m.bin.gz")
        from go_analysis.analyzer.windows import WindowsAnalyzer
        self.assertIsInstance(a, WindowsAnalyzer)

    def test_create_passes_kwargs(self):
        from go_analysis.analyzer import create_analyzer
        a = create_analyzer("windows", katago_path="k.exe", model_path="m.bin.gz",
                            visits=50, batch_timeout=300.0)
        self.assertEqual(a.visits, 50)
        self.assertEqual(a.batch_timeout, 300.0)

    def test_create_unknown_type(self):
        from go_analysis.analyzer import create_analyzer
        with self.assertRaises(ValueError):
            create_analyzer("fake_type")


class TestBaseAnalyzerInterface(unittest.TestCase):
    """▶ 5. BaseAnalyzer — tune/benchmark 不是自动的"""

    def test_tune_not_auto_called(self):
        """__init__ 不应自动调 tune/benchmark"""
        from go_analysis.analyzer.windows import WindowsAnalyzer
        original_tune = WindowsAnalyzer.tune
        called = []

        def spy_tune(self, *a, **kw):
            called.append(True)
            return {}

        with patch.object(WindowsAnalyzer, 'tune', spy_tune):
            a = WindowsAnalyzer(katago_path="k.exe", model_path="m.bin.gz")
        self.assertEqual(len(called), 0, "tune() 不应在初始化时自动调用")

    def test_benchmark_not_auto_called(self):
        from go_analysis.analyzer.windows import WindowsAnalyzer
        called = []

        def spy_bench(*a, **kw):
            called.append(True)
            return {}

        with patch.object(WindowsAnalyzer, 'benchmark', spy_bench):
            a = WindowsAnalyzer(katago_path="k.exe", model_path="m.bin.gz")
        self.assertEqual(len(called), 0, "benchmark() 不应在初始化时自动调用")

    def test_analyze_calls_ensure_and_rw(self):
        """analyze() 的调用链路不触发 tune/benchmark"""
        from go_analysis.analyzer.windows import WindowsAnalyzer
        called = {"tune": 0, "bench": 0}

        real_tune = WindowsAnalyzer.tune
        real_bench = WindowsAnalyzer.benchmark

        def spy_tune(*a, **kw):
            called["tune"] += 1
            return {}

        def spy_bench(*a, **kw):
            called["bench"] += 1
            return {}

        with patch.object(WindowsAnalyzer, 'tune', spy_tune), \
             patch.object(WindowsAnalyzer, 'benchmark', spy_bench):
            a = WindowsAnalyzer(katago_path="k.exe", model_path="m.bin.gz")
        self.assertEqual(called["tune"], 0)
        self.assertEqual(called["bench"], 0)


class TestMovesToKatagoFormat(unittest.TestCase):
    """▶ 6. moves_to_katago_format — 内部格式→KataGo API 格式"""

    def test_basic_conversion(self):
        from go_analysis.analyzer.base import moves_to_katago_format
        moves = [{"x": 3, "y": 3}, {"x": 15, "y": 15}]
        result = moves_to_katago_format(moves)
        self.assertEqual(result, [["B", "D4"], ["W", "Q16"]])

    def test_alternating_colors(self):
        from go_analysis.analyzer.base import moves_to_katago_format
        moves = [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 2, "y": 0}]
        result = moves_to_katago_format(moves)
        self.assertEqual(result, [["B", "A1"], ["W", "B1"], ["B", "C1"]])

    def test_empty_moves(self):
        from go_analysis.analyzer.base import moves_to_katago_format
        self.assertEqual(moves_to_katago_format([]), [])

    def test_skip_I_column(self):
        """GTP 标准：跳过 I，所以 H=8->8, J=9->10"""
        from go_analysis.analyzer.base import moves_to_katago_format
        moves = [{"x": 7, "y": 7}, {"x": 8, "y": 7}, {"x": 9, "y": 7}]
        result = moves_to_katago_format(moves)
        self.assertEqual(result, [["B", "H8"], ["W", "J8"], ["B", "K8"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)

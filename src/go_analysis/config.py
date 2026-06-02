"""
配置管理器 — YAML + ENV + 默认值 三层覆盖。

优先级: CLI 参数 > 环境变量 > YAML 文件 > 默认值

用法::

    from go_analysis.config import ConfigManager

    cfg = ConfigManager()
    visits = cfg.get('analyzer.visits', 96)
    platform = cfg.get('analyzer.default_platform', 'auto')
"""

import os
import yaml
from pathlib import Path
from typing import Any


# 默认配置 (最低优先级)
DEFAULT_CONFIG = {
    "analyzer": {
        "default_platform": "auto",         # auto | windows_native | ssh | http
        "visits": 96,
        "visits_smart": True,               # PERF: 智能 visits 选择
        "visits_prototype": 25,              # PERF: 原型验证用低 visits
        "visits_batch": 50,                  # PERF: 批量处理 visits
        "visits_precision": 96,              # PERF: 精确分析 visits
        "num_analysis_threads": 2,
        "num_search_threads": 16,
        "nn_max_batch_size": 8,
        "gpu_devices": [0],                  # PERF: GPU 设备列表, [] = CPU
        "parallel_engines": 1,               # PERF: 并行 KataGo 引擎数
    },
    "storage": {
        "backend": "file",                   # file | sqlite | postgres
        "path": "./analysis_store",
        "compress": True,
    },
    "model": {
        "name": "go-strength-v2",
        "input_dim": 12,
        "hidden_dim": 256,
        "num_heads": 4,
        "num_layers": 3,
        "num_ranks": 9,
    },
    "training": {
        "batch_size": 32,
        "learning_rate": 0.001,
        "epochs": 100,
        "early_stopping": 10,
        "incremental": False,
        "resume_checkpoint": None,
    },
    "hosts": [],
    "logging": {
        "level": "INFO",
        "file": None,
    },
}


class ConfigManager:
    """配置管理器 — 全局单例"""

    _instance = None
    _frozen = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config = {}
            cls._instance._config_files = []
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._config = DEFAULT_CONFIG.copy()
        self._config_files = []
        self._frozen = False

    # ── 加载 ────────────────────────────────────────

    def load_yaml(self, path: str | Path) -> "ConfigManager":
        """从 YAML 文件加载 (第二优先级)"""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")
        with open(path) as f:
            data = yaml.safe_load(f)
        if data:
            self._deep_merge(self._config, data)
            self._config_files.append(str(path))
        return self

    def load_env(self, prefix: str = "GO_ANALYZER_") -> "ConfigManager":
        """从环境变量加载 (第二优先级)"""
        for key, val in sorted(os.environ.items()):
            if not key.startswith(prefix):
                continue
            parts = key[len(prefix):].lower().split("__")
            target = self._config
            for part in parts[:-1]:
                if part not in target:
                    target[part] = {}
                target = target[part]
            target[parts[-1]] = self._coerce(val)
        return self

    def update(self, overrides: dict) -> "ConfigManager":
        """运行时覆盖 (最高优先级)"""
        self._deep_merge(self._config, overrides)
        return self

    # ── 读取 ────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """点号路径访问: cfg.get('analyzer.visits')"""
        parts = key.split(".")
        target = self._config
        for part in parts:
            if isinstance(target, dict) and part in target:
                target = target[part]
            else:
                return default
        return target

    def set(self, key: str, value: Any):
        """运行时设置 (未冻结时)"""
        if self._frozen:
            raise RuntimeError("Config is frozen, cannot modify")
        parts = key.split(".")
        target = self._config
        for part in parts[:-1]:
            if part not in target:
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value

    @property
    def hosts(self) -> list[dict]:
        """注册的分析主机列表"""
        return self._config.get("hosts", [])

    def freeze(self):
        """冻结配置 (训练开始后调用)"""
        self._frozen = True

    def to_dict(self) -> dict:
        """导出为字典"""
        return self._config.copy()

    # ── 内部 ────────────────────────────────────────

    def _deep_merge(self, base: dict, override: dict):
        """递归合并字典"""
        for key, val in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                self._deep_merge(base[key], val)
            else:
                base[key] = val

    def _coerce(self, val: str) -> Any:
        """环境变量字符串 → Python 类型"""
        if val.lower() in ("true", "yes", "1"):
            return True
        if val.lower() in ("false", "no", "0"):
            return False
        if val.lower() == "none":
            return None
        try:
            return int(val)
        except ValueError:
            pass
        try:
            return float(val)
        except ValueError:
            pass
        return val


# ── 快速入口 ────────────────────────────────────────

def load_config(path: str | Path = None) -> ConfigManager:
    """一行加载配置"""
    cfg = ConfigManager()
    cfg.load_env()
    if path:
        cfg.load_yaml(path)
    return cfg

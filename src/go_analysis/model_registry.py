"""
模型注册表 — 版本管理 + 导出 + 部署。

目录结构::

    models/
    ├── v1/
    │   ├── model.pt          # PyTorch 权重
    │   ├── config.yaml       # 训练配置
    │   ├── metrics.json      # 评估指标
    │   └── calibrator.pkl    # 校准器
    ├── v2/ ...
    └── latest -> v2          # 符号链接到最新
"""

import json
import shutil
from pathlib import Path
from typing import Any
from datetime import datetime

import yaml


class ModelRegistry:
    """模型注册表 — 版本管理"""

    def __init__(self, models_dir: str | Path = "./models"):
        self._base = Path(models_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    # ── 版本操作 ────────────────────────────────────

    def save(self, name: str, model, metrics: dict[str, Any],
             config: dict[str, Any], calibrator=None):
        """保存模型版本"""
        version_dir = self._base / name
        version_dir.mkdir(parents=True, exist_ok=True)

        # 保存 PyTorch 权重
        import torch
        torch.save(model.state_dict(), version_dir / "model.pt")

        # 保存配置
        with open(version_dir / "config.yaml", "w") as f:
            yaml.dump(config, f)

        # 保存指标
        metrics["saved_at"] = datetime.utcnow().isoformat()
        with open(version_dir / "metrics.json", "w") as f:
            json.dump(metrics, f, indent=2)

        # 保存校准器
        if calibrator:
            import pickle
            with open(version_dir / "calibrator.pkl", "wb") as f:
                pickle.dump(calibrator, f)

        # 更新 latest 符号链接
        latest = self._base / "latest"
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(name)

        return name

    def load(self, version: str = "latest"):
        """加载模型版本"""
        version_dir = self._resolve(version)

        # 加载配置
        with open(version_dir / "config.yaml") as f:
            config = yaml.safe_load(f)

        # 加载模型
        from go_analysis.model_v2 import GoStrengthModel
        model = GoStrengthModel(**config.get("model", {}))
        import torch
        model.load_state_dict(torch.load(version_dir / "model.pt"))
        model.eval()

        # 加载校准器
        calibrator = None
        cal_path = version_dir / "calibrator.pkl"
        if cal_path.exists():
            import pickle
            with open(cal_path, "rb") as f:
                calibrator = pickle.load(f)

        # 加载指标
        metrics = {}
        metrics_path = version_dir / "metrics.json"
        if metrics_path.exists():
            with open(metrics_path) as f:
                metrics = json.load(f)

        return model, calibrator, config, metrics

    def list_versions(self) -> list[dict]:
        """列出所有版本"""
        versions = []
        for d in sorted(self._base.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            metrics = {}
            metrics_path = d / "metrics.json"
            if metrics_path.exists():
                with open(metrics_path) as f:
                    metrics = json.load(f)

            is_latest = d.resolve() == (self._base / "latest").resolve()
            versions.append({
                "name": d.name,
                "latest": is_latest,
                "saved_at": metrics.get("saved_at", "?"),
                "metrics": metrics,
            })
        return sorted(versions, key=lambda v: v["name"])

    def rollback(self, version: str):
        """回滚到指定版本"""
        version_dir = self._resolve(version)
        if not version_dir.exists():
            raise FileNotFoundError(f"Version not found: {version}")
        latest = self._base / "latest"
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(version_dir.name)

    # ── 导出 ────────────────────────────────────────

    def export(self, version: str, format: str = "onnx",
               output_dir: str | Path = "./export"):
        """导出模型为部署格式"""
        model, calibrator, config, metrics = self.load(version)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if format == "onnx":
            self._export_onnx(model, config, output_dir)
        elif format == "torchscript":
            self._export_torchscript(model, output_dir)
        elif format == "pt":
            self._export_pt(model, output_dir)
        else:
            raise ValueError(f"Unsupported format: {format}")

        # 同时导出配置和校准器
        shutil.copy2(self._resolve(version) / "config.yaml", output_dir / "config.yaml")
        cal_path = self._resolve(version) / "calibrator.pkl"
        if cal_path.exists():
            shutil.copy2(cal_path, output_dir / "calibrator.pkl")

        return str(output_dir)

    def _export_onnx(self, model, config, output_dir):
        import torch
        dummy = torch.randn(1, config.get("model", {}).get("input_dim", 12))
        torch.onnx.export(model, dummy, str(output_dir / "model.onnx"),
                          input_names=["features"],
                          output_names=["logits"],
                          dynamic_axes={"features": {0: "batch"}})

    def _export_torchscript(self, model, output_dir):
        import torch
        traced = torch.jit.script(model)
        traced.save(str(output_dir / "model.pt"))

    def _export_pt(self, model, output_dir):
        import torch
        torch.save(model.state_dict(), output_dir / "model.pt")

    # ── 内部 ────────────────────────────────────────

    def _resolve(self, version: str) -> Path:
        p = self._base / version
        if p.is_symlink():
            return p.resolve()
        return p

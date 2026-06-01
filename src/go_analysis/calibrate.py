"""
段位校准模块 (Label Distribution Matching).

核心思想：用外部标准棋谱的预测分布来校正内部标签系统。
当训练集和测试集来自不同段位分布时，通过分布匹配消除偏差。

参考 DS plan 第五步思路，实现可用的校准管道。
"""

import numpy as np
import torch
from torch.utils.data import DataLoader

from .dataset import GoDataset, collate_padded
from .model import GoStrengthModel


class LabelCalibrator:
    """校准预测的段位标签。

    从外部标准数据集上计算模型输出的期望分布，然后映射
    内部标签使预测分布对齐。（Label Distribution Matching）

    Parameters
    ----------
    n_classes : int
        段位类别数 (默认 5: 3d~7d)
    """

    def __init__(self, n_classes: int = 5):
        self.n_classes = n_classes
        self.mapping: np.ndarray = None   # (n_classes, n_classes) 线性映射矩阵

    def fit(
        self,
        model: GoStrengthModel,
        external_loader: DataLoader,
        device: torch.device = None,
    ):
        """在外部标准数据集上拟合校准映射。

        收集每个真实段位下模型的平均预测分布作为 centroid，
        以这些 centroid 作为目标，拟合线性映射。
        """
        if device is None:
            device = next(model.parameters()).device

        model.eval()
        label_probs = {i: [] for i in range(self.n_classes)}

        with torch.no_grad():
            for seqs, masks, globals_, labels in external_loader:
                seqs = seqs.to(device)
                masks = masks.to(device)
                globals_ = globals_.to(device)

                probs = model(seqs, globals_, mask=masks).cpu().numpy()

                for i in range(len(labels)):
                    lbl = labels[i].item()
                    if lbl in label_probs:
                        label_probs[lbl].append(probs[i])

        # 计算 centroid: 每个标签的平均预测分布
        external_centroids = np.zeros((self.n_classes, self.n_classes))
        for lbl in range(self.n_classes):
            pts = label_probs.get(lbl, [])
            if pts:
                external_centroids[lbl] = np.mean(pts, axis=0)
            else:
                external_centroids[lbl, lbl] = 1.0  # fallback: identity

        # 拟合线性映射: 期望 external = internal @ mapping
        # internal_centroids 是单位矩阵 (假设内部标签均匀)
        internal_centroids = np.eye(self.n_classes)

        # 最小二乘: mapping = pinv(internal) @ external
        self.mapping = np.linalg.lstsq(
            internal_centroids, external_centroids, rcond=None
        )[0]

        # 确保每行和为 1 (概率分布)
        self.mapping = self.mapping / self.mapping.sum(axis=1, keepdims=True)

        print(f"[Calibrator] Fitted mapping:\n{self.mapping.round(3)}")
        return self

    def calibrate(self, probs: np.ndarray) -> np.ndarray:
        """应用校准映射到预测概率。

        Parameters
        ----------
        probs : (batch, n_classes)
            原始预测概率 (模型输出)

        Returns
        -------
        calibrated : (batch, n_classes)
            校准后的概率分布
        """
        if self.mapping is None:
            raise RuntimeError("Calibrator not fitted. Call fit() first.")
        return probs @ self.mapping.T

    def predict(self, probs: np.ndarray) -> np.ndarray:
        """返回校准后的段位预测 (整数 0..n_classes-1)。"""
        calibrated = self.calibrate(probs)
        return calibrated.argmax(axis=1)


def calibrate_centroid_shift(
    internal_probs: np.ndarray,
    external_probs: np.ndarray,
    labels: np.ndarray,
) -> np.ndarray:
    """简单偏移校: 对每个标签，计算内部/外部 centroid 差并修正。

    更轻量的方法，不需要线性映射矩阵。

    Parameters
    ----------
    internal_probs : (N, n_classes) 内部数据预测
    external_probs : (M, n_classes) 外部标准数据预测
    labels : (N,) 内部数据的真实标签

    Returns
    -------
    adjusted_labels : (N,) 调整后的预测标签
    """
    n_classes = internal_probs.shape[1]
    internal_centroids = np.zeros((n_classes, n_classes))
    for lbl in range(n_classes):
        mask = labels == lbl
        if mask.sum() > 0:
            internal_centroids[lbl] = internal_probs[mask].mean(axis=0)

    external_centroids = np.zeros((n_classes, n_classes))
    for lbl in range(n_classes):
        external_centroids[lbl] = external_probs[external_probs.argmax(axis=1) == lbl].mean(axis=0)

    # 对每个样本，找到最近的 centroid 偏移对
    adjusted = np.zeros(len(internal_probs), dtype=int)
    for i in range(len(internal_probs)):
        raw_pred = internal_probs[i].argmax()
        # 计算偏移量
        delta = internal_probs[i] - internal_centroids[raw_pred]
        adjusted_prob = external_centroids[raw_pred] + delta
        adjusted[i] = adjusted_prob.argmax() if adjusted_prob.max() > 0 else raw_pred

    return adjusted

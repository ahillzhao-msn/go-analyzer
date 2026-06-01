"""
Transformer + ordinal regression model for Go strength prediction.

Architecture:
  - Per-move feature projection (10 → d_model)
  - Positional encoding (sin/cos)
  - Transformer encoder (self-attention over move sequence)
  - Attentive pooling → game-level embedding
  - Fusion with global stats
  - Ordinal logistic regression head
"""

import math

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np


class PositionalEncoding(nn.Module):
    """Sinusoidal positional encoding for variable-length sequences."""

    def __init__(self, d_model: int, max_len: int = 500):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2).float()
            * (-math.log(10000.0) / d_model)
        )
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        self.register_buffer("pe", pe.unsqueeze(0))  # (1, max_len, d_model)

    def forward(self, x):
        # x: (batch, seq_len, d_model)
        return x + self.pe[:, : x.size(1), :]


class OrdinalLogisticHead(nn.Module):
    """Ordinal logistic regression head with learnable thresholds.

    Predicts P(y = k | x) for k = 0..n_classes-1 using cumulative logits.
    """

    def __init__(self, input_dim: int, n_classes: int = 5):
        super().__init__()
        self.linear = nn.Linear(input_dim, 1)
        # Learnable thresholds: -1.5, -0.75, 0.0, 0.75, 1.5 for 5 classes
        self.thresholds = nn.Parameter(
            torch.linspace(-1.5, 1.5, n_classes - 1)
        )

    def forward(self, x):
        """Return class probabilities.

        Parameters
        ----------
        x : (batch, input_dim)

        Returns
        -------
        probs : (batch, n_classes)
        """
        theta = self.linear(x).squeeze(-1)  # (batch,)

        # For each threshold k, compute logit_k = theta - b_k
        # P(Y <= k) = sigmoid(b_k - theta)
        # P(Y = k) = sigmoid(b_k - theta) - sigmoid(b_{k-1} - theta)
        thresholds = self.thresholds  # (n_classes-1,)

        # Compute cumulative probabilities
        cum_probs = torch.sigmoid(thresholds.unsqueeze(0) - theta.unsqueeze(1))  # (batch, n_classes-1)

        # Derive class probabilities
        n_classes = len(thresholds) + 1
        probs = torch.zeros(x.size(0), n_classes, device=x.device)
        probs[:, 0] = cum_probs[:, 0]
        for k in range(1, n_classes - 1):
            probs[:, k] = cum_probs[:, k] - cum_probs[:, k - 1]
        probs[:, n_classes - 1] = 1.0 - cum_probs[:, -1]

        # Clamp for numerical stability
        probs = torch.clamp(probs, min=1e-8, max=1.0 - 1e-8)
        return probs

    def get_ordinal_logits(self, x):
        """Return raw ordinal logits (theta value) for custom loss."""
        return self.linear(x).squeeze(-1)

    def get_ordinal_loss(self, theta: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        """Ordinal negative log-likelihood loss.

        Parameters
        ----------
        theta : (batch,) — latent score from linear layer
        y : (batch,) — integer labels 0..n_classes-1

        Returns
        -------
        loss : scalar tensor
        """
        thresholds = self.thresholds  # (n_classes-1,)
        n_classes = len(thresholds) + 1
        y = y.clamp(0, n_classes - 1)

        # For each class k, compute log P(Y = k | theta)
        # Using: log P(Y <= k) - log P(Y <= k-1)
        # where P(Y <= k) = sigmoid(b_k - theta)
        cum_logits = thresholds.unsqueeze(0) - theta.unsqueeze(1)  # (batch, n_classes-1)
        cum_probs = torch.sigmoid(cum_logits)

        # P(Y = 0) = P(Y <= 0)
        # P(Y = k) = P(Y <= k) - P(Y <= k-1)  for 0 < k < n_classes-1
        # P(Y = n-1) = 1 - P(Y <= n-2)
        probs = torch.zeros_like(
            theta.unsqueeze(1).expand(-1, n_classes)
        )
        probs[:, 0] = cum_probs[:, 0]
        for k in range(1, n_classes - 1):
            probs[:, k] = cum_probs[:, k] - cum_probs[:, k - 1]
        probs[:, n_classes - 1] = 1.0 - cum_probs[:, -1]
        probs = torch.clamp(probs, min=1e-8)

        # Negative log-likelihood
        loss = F.nll_loss(torch.log(probs), y)
        return loss


class GoStrengthModel(nn.Module):
    """Transformer-based Go strength model with ordinal regression.

    Parameters
    ----------
    input_dim : int
        Per-move feature dimension (default 10).
    d_model : int
        Transformer embedding dimension.
    nhead : int
        Number of attention heads.
    num_layers : int
        Number of transformer encoder layers.
    max_len : int
        Maximum sequence length for positional encoding.
    n_classes : int
        Number of ordinal output classes.
    """

    def __init__(
        self,
        input_dim: int = 12,
        d_model: int = 128,
        nhead: int = 8,
        num_layers: int = 4,
        max_len: int = 400,
        n_classes: int = 5,
        dropout: float = 0.1,
    ):
        super().__init__()

        self.input_proj = nn.Linear(input_dim, d_model)
        self.pos_encoder = PositionalEncoding(d_model, max_len)
        self.dropout = nn.Dropout(dropout)

        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model,
            nhead=nhead,
            dim_feedforward=d_model * 4,
            dropout=dropout,
            batch_first=True,
            norm_first=True,
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)

        # Pooling heads
        self.pool_proj = nn.Linear(d_model, d_model)
        self.global_stats_proj = nn.Linear(12, d_model)
        self.combine = nn.Linear(d_model * 2, d_model)

        # Output head
        self.output_head = OrdinalLogisticHead(d_model, n_classes)

        self._init_weights()

    def _init_weights(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def _encode(self, x, global_stats, mask=None):
        """Shared encoding: returns the fused game-level representation."""
        x = self.input_proj(x)              # (B, T, d_model)
        x = self.pos_encoder(x)
        x = self.dropout(x)
        x = self.transformer(x, src_key_padding_mask=mask)

        if mask is not None:
            valid = (~mask).unsqueeze(-1).float()
            seq_avg = (x * valid).sum(dim=1) / valid.sum(dim=1).clamp(min=1)
        else:
            seq_avg = x.mean(dim=1)

        seq_avg = F.relu(self.pool_proj(seq_avg))
        global_feat = F.relu(self.global_stats_proj(global_stats))
        combined = torch.cat([seq_avg, global_feat], dim=-1)
        combined = F.relu(self.combine(combined))
        return combined

    def forward(self, x, global_stats, mask=None):
        """Return class probabilities.

        Parameters
        ----------
        x : (batch, seq_len, input_dim)
        global_stats : (batch, 12)
        mask : (batch, seq_len) or None

        Returns
        -------
        probs : (batch, n_classes)
        """
        combined = self._encode(x, global_stats, mask)
        return self.output_head(combined)

    def forward_details(self, x, global_stats, mask=None):
        """Return (probs, theta) for training with ordinal loss.

        theta is the latent score used inside OrdinalLogisticHead.
        """
        combined = self._encode(x, global_stats, mask)
        probs = self.output_head(combined)
        theta = self.output_head.get_ordinal_logits(combined)
        return probs, theta

    def predict_theta(self, x, global_stats, mask=None):
        """Return the latent ordinal score (theta) for calibration."""
        combined = self._encode(x, global_stats, mask)
        return self.output_head.get_ordinal_logits(combined)

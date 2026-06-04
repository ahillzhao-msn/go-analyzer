"""evaluation/model.py — GoStrengthModel v2

黑白分离 Causal Self-Attention 架构。
包含 5 个类: JointEncoding, CosineCrossAttention, AttentionPooling,
           OrdinalLogisticHead, GoStrengthModel
"""
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

__version__ = "2.0.0"
__arch__ = "bw_separated_causal_crossattn"


# ── 位置编码 (增强版) ──────────────────────────────────

class JointEncoding(nn.Module):
    """联合位置编码: 绝对位置 + 剩余自由度。

    随着棋局进展，剩余自由度的减少比单纯位置计数更有信息量。
    编码: PE(pos) + PE(remaining_moves / total_moves)
    """
    def __init__(self, d_model: int, max_len: int = 400):
        super().__init__()
        self.d_model = d_model
        self.max_len = max_len
        pe = torch.zeros(max_len, d_model)
        pos = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(pos * div)
        pe[:, 1::2] = torch.cos(pos * div)
        self.register_buffer("pe", pe.unsqueeze(0))

    def forward(self, x, remaining_ratio: torch.Tensor = None):
        """x: (B, T, d_model), remaining_ratio: (B, T) or None"""
        # 绝对位置编码
        pos_enc = self.pe[:, :x.size(1), :]

        if remaining_ratio is not None:
            # 剩余自由度编码: encoding at full d_model/2
            ratio = remaining_ratio.unsqueeze(-1)  # (B, T, 1)
            # Scale ratio to [-2, 2] range for sin/cos
            ratio_enc = torch.cat([
                torch.sin(ratio * self.pe[:, :x.size(1), 0::2]),
                torch.cos(ratio * self.pe[:, :x.size(1), 1::2]),
            ], dim=-1)
            # Trim or pad to match d_model
            if ratio_enc.size(-1) > self.d_model:
                ratio_enc = ratio_enc[:, :, :self.d_model]
            elif ratio_enc.size(-1) < self.d_model:
                ratio_enc = F.pad(ratio_enc, (0, self.d_model - ratio_enc.size(-1)))
            return x + pos_enc + ratio_enc

        return x + pos_enc


# ── 余弦交叉注意力 ────────────────────────────────────

class CosineCrossAttention(nn.Module):
    """黑白交叉注意力: 用余弦相似度计算互影响。

    每手棋与对方每手棋计算余弦相似度做注意力。
    模拟"复盘"——回头看对方每手对自己这手的影响。
    """
    def __init__(self, d_model: int):
        super().__init__()
        self.proj_q = nn.Linear(d_model, d_model)
        self.proj_k = nn.Linear(d_model, d_model)
        self.proj_v = nn.Linear(d_model, d_model)
        self.out_proj = nn.Linear(d_model, d_model)

    def forward(self, x_b, x_w):
        """x_b: (B, T_b, d_model), x_w: (B, T_w, d_model)
           Returns: (B, T_b + T_w, d_model) cross-attended
        """
        B, T_b, D = x_b.shape
        T_w = x_w.size(1)

        # Q from B, K,V from W: B asks "how does each W move affect me?"
        q = self.proj_q(x_b)    # (B, T_b, D)
        k = self.proj_k(x_w)    # (B, T_w, D)
        v = self.proj_v(x_w)    # (B, T_w, D)

        # Cosine similarity attention: q·k / (|q||k|)
        q_norm = F.normalize(q, dim=-1)   # (B, T_b, D)
        k_norm = F.normalize(k, dim=-1)   # (B, T_w, D)
        attn = torch.bmm(q_norm, k_norm.transpose(1, 2))  # (B, T_b, T_w)

        # Apply temporal causality: B's i-th move can only see W's first i moves
        # (since W's j-th move was played AFTER B's j-th move but BEFORE B's (j+1)-th)
        # More precisely: B move i interacts with W moves through i-1
        mask = torch.triu(torch.ones(T_b, T_w, device=x_b.device), diagonal=0).unsqueeze(0)
        attn = attn.masked_fill(mask[:, :T_b, :T_w] == 0, float('-inf'))

        attn_weights = F.softmax(attn, dim=-1)  # (B, T_b, T_w)

        # Handle all-masked rows: replace NaN with uniform
        row_sum = attn_weights.sum(dim=-1, keepdim=True)
        attn_weights = torch.where(row_sum > 0, attn_weights, torch.ones_like(attn_weights) / T_w)

        # Value aggregation
        attended = torch.bmm(attn_weights, v)  # (B, T_b, D)
        attended = self.out_proj(attended)

        # Combine original + cross-attended
        x_b_cross = x_b + attended

        # Same for W → B
        q_w = self.proj_q(x_w)
        k_b = self.proj_k(x_b)
        v_b = self.proj_v(x_b)

        q_w_norm = F.normalize(q_w, dim=-1)
        k_b_norm = F.normalize(k_b, dim=-1)
        attn_w = torch.bmm(q_w_norm, k_b_norm.transpose(1, 2))

        mask_w = torch.triu(torch.ones(T_w, T_b, device=x_w.device), diagonal=1).unsqueeze(0)
        attn_w = attn_w.masked_fill(mask_w[:, :T_w, :T_b] == 0, float('-inf'))

        attn_weights_w = F.softmax(attn_w, dim=-1)

        # Handle all-masked rows
        row_sum_w = attn_weights_w.sum(dim=-1, keepdim=True)
        attn_weights_w = torch.where(row_sum_w > 0, attn_weights_w, torch.ones_like(attn_weights_w) / T_b)

        attended_w = torch.bmm(attn_weights_w, v_b)
        attended_w = self.out_proj(attended_w)

        x_w_cross = x_w + attended_w

        # Interleave back to original order
        # This assumes B starts
        total = T_b + T_w
        out = torch.zeros(B, total, D, device=x_b.device)
        out[:, 0::2, :] = x_b_cross
        out[:, 1::2, :] = x_w_cross[:, :total//2, :]

        return out


# ── 注意力池化 ────────────────────────────────────────

class AttentionPooling(nn.Module):
    """可学习的注意力池化: 模型自己决定哪些位置重要。"""
    def __init__(self, d_model: int):
        super().__init__()
        self.query = nn.Parameter(torch.randn(d_model) * 0.02)
        self.proj = nn.Linear(d_model, d_model)

    def forward(self, x, mask=None):
        """x: (B, T, d_model), mask: (B, T) bool (True=pad)
           Returns: (B, d_model)
        """
        h = torch.tanh(self.proj(x))  # (B, T, D)
        scores = torch.einsum('btd,d->bt', h, self.query)  # (B, T)
        if mask is not None:
            scores = scores.masked_fill(mask, float('-inf'))
        weights = F.softmax(scores, dim=-1).unsqueeze(-1)  # (B, T, 1)
        return (weights * x).sum(dim=1)  # (B, D)


# ── 序数回归头 ─────────────────────────────────────────

class OrdinalLogisticHead(nn.Module):
    """与 v1 相同。"""
    def __init__(self, input_dim: int, n_classes: int = 5):
        super().__init__()
        self.linear = nn.Linear(input_dim, 1)
        self.thresholds = nn.Parameter(torch.linspace(-1.5, 1.5, n_classes - 1))

    def forward(self, x):
        theta = self.linear(x).squeeze(-1)
        cum_probs = torch.sigmoid(self.thresholds.unsqueeze(0) - theta.unsqueeze(1))
        n_classes = len(self.thresholds) + 1
        probs = torch.zeros(x.size(0), n_classes, device=x.device)
        probs[:, 0] = cum_probs[:, 0]
        for k in range(1, n_classes - 1):
            probs[:, k] = cum_probs[:, k] - cum_probs[:, k - 1]
        probs[:, n_classes - 1] = 1.0 - cum_probs[:, -1]
        probs = torch.clamp(probs, min=1e-8, max=1.0 - 1e-8)
        return probs

    def get_ordinal_logits(self, x):
        return self.linear(x).squeeze(-1)

    def get_ordinal_loss(self, theta: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        thresholds = self.thresholds
        n_classes = len(thresholds) + 1
        y = y.clamp(0, n_classes - 1)
        cum_logits = thresholds.unsqueeze(0) - theta.unsqueeze(1)
        cum_probs = torch.sigmoid(cum_logits)
        probs = torch.zeros_like(theta.unsqueeze(1).expand(-1, n_classes))
        probs[:, 0] = cum_probs[:, 0]
        for k in range(1, n_classes - 1):
            probs[:, k] = cum_probs[:, k] - cum_probs[:, k - 1]
        probs[:, n_classes - 1] = 1.0 - cum_probs[:, -1]
        probs = torch.clamp(probs, min=1e-8)
        loss = F.nll_loss(torch.log(probs), y)
        return loss


# ── 主模型 ─────────────────────────────────────────────

class GoStrengthModel(nn.Module):
    """GoStrengthModel v2 — 黑白分离 + 因果 + 余弦交叉注意力。

    Parameters
    ----------
    input_dim : int
        Per-move feature dimension (default 12, will be split B/W internally).
    d_model : int
        Transformer embedding dimension.
    nhead : int
        Number of attention heads.
    num_layers : int
        Number of transformer encoder layers (per stream).
    n_classes : int
        Number of ordinal output classes.
    env_dim : int
        Environment vector dimension (Global=12 + HW + SW + Game meta).
    """

    def __init__(
        self,
        input_dim: int = 12,
        d_model: int = 128,
        nhead: int = 8,
        num_layers: int = 2,
        n_classes: int = 5,
        env_dim: int = 12,
        dropout: float = 0.1,
    ):
        super().__init__()

        self.d_model = d_model
        self.env_dim = env_dim
        self.input_dim = input_dim

        # 共享投影 (黑白共享权重 — 因为特征空间相同)
        self.input_proj = nn.Linear(input_dim, d_model)

        # 联合位置编码 (绝对位置 + 剩余自由度)
        self.joint_enc = JointEncoding(d_model)

        # Dropout
        self.dropout = nn.Dropout(dropout)

        # 黑白独立 Self-Attention (Causal Mask)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead,
            dim_feedforward=d_model * 4,
            dropout=dropout, batch_first=True, norm_first=True,
        )
        self.self_attn_b = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
        self.self_attn_w = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)

        # 余弦交叉注意力 (复盘)
        self.cross_attn = CosineCrossAttention(d_model)

        # 注意力池化 (学权重)
        self.pooling = AttentionPooling(d_model)

        # 环境向量融合
        self.env_proj = nn.Linear(env_dim, d_model)
        self.combine = nn.Linear(d_model * 2, d_model)

        # 输出头
        self.output_head = OrdinalLogisticHead(d_model, n_classes)

        self._init_weights()

    def _init_weights(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def _split_bw(self, x, player_dim=11):
        """将混合序列按黑白分离。

        x: (B, T, D_in) where x[:,:,11] = 0 for black, 1 for white
        Returns: (B, T_b, D), (B, T_w, D)
        """
        B, T, D = x.shape

        # Create masks for black and white positions
        # player_dim is the index of the player feature (dim 11 in 12-dim input)
        is_black = (x[:, :, player_dim] < 0.5).unsqueeze(-1).float()  # (B, T, 1)
        is_white = 1.0 - is_black

        # Project and mask
        x_proj = self.input_proj(x)  # (B, T, d_model)

        # Split (we know the sequence starts with B, so we can use strides)
        # Simplest: assume interleaved B,W,B,W...
        x_b = x_proj[:, 0::2, :]   # (B, T_b, d_model)
        x_w = x_proj[:, 1::2, :]   # (B, T_w, d_model)

        return x_b, x_w

    def _compute_remaining(self, x_b, x_w):
        """计算剩余自由度比率。"""
        B, T_b, _ = x_b.shape
        T_w = x_w.size(1)
        total = T_b + T_w

        # For B stream: remaining = (total - 2*i) / total
        idx_b = torch.arange(T_b, device=x_b.device).float().unsqueeze(0).expand(B, -1)
        remaining_b = (total - 2 * idx_b).clamp(min=0) / total

        idx_w = torch.arange(T_w, device=x_w.device).float().unsqueeze(0).expand(B, -1)
        remaining_w = (total - (2 * idx_w + 1)).clamp(min=0) / total

        return remaining_b, remaining_w

    def _causal_mask(self, size, device):
        """生成因果掩码: 上三角。"""
        return torch.triu(torch.ones(size, size, device=device) * float('-inf'), diagonal=1)

    def forward(self, x, env_vec, mask=None):
        """Forward pass.

        Parameters
        ----------
        x : (B, T, input_dim)
            每手特征序列 (混合 B/W)
        env_vec : (B, env_dim) or None
            环境向量 (Global+HW+SW+Game)
        mask : (B, T) or None
            padding mask (True=pad)

        Returns
        -------
        probs : (B, n_classes)
        """
        # 1. 分离黑白
        x_b, x_w = self._split_bw(x)

        # 2. 剩余自由度编码
        rem_b, rem_w = self._compute_remaining(x_b, x_w)

        x_b = self.joint_enc(x_b, rem_b)
        x_w = self.joint_enc(x_w, rem_w)
        x_b = self.dropout(x_b)
        x_w = self.dropout(x_w)

        # 3. 因果自注意力 (每方只看到自己的历史)
        T_b, T_w = x_b.size(1), x_w.size(1)
        causal_b = self._causal_mask(T_b, x_b.device)
        causal_w = self._causal_mask(T_w, x_w.device)

        x_b = self.self_attn_b(x_b, mask=causal_b)
        x_w = self.self_attn_w(x_w, mask=causal_w)

        # 4. 余弦交叉注意力 (复盘)
        x_review = self.cross_attn(x_b, x_w)  # (B, T_b+T_w, d_model)

        # 5. 注意力池化
        if mask is not None:
            # 把 B/W split 的 mask 重新合并
            vec = self.pooling(x_review, mask=mask)
        else:
            vec = self.pooling(x_review)

        # 6. 环境向量融合
        if env_vec is not None:
            env_feat = F.relu(self.env_proj(env_vec))
        else:
            env_feat = torch.zeros_like(vec)

        combined = torch.cat([vec, env_feat], dim=-1)
        combined = F.relu(self.combine(combined))

        # 7. 序数输出
        return self.output_head(combined)

    def forward_details(self, x, env_vec, mask=None):
        combined = self._encode(x, env_vec, mask)
        return self.output_head(combined), self.output_head.get_ordinal_logits(combined)

    def _encode(self, x, env_vec, mask=None):
        """共享编码逻辑。"""
        x_b, x_w = self._split_bw(x)
        rem_b, rem_w = self._compute_remaining(x_b, x_w)

        x_b = self.joint_enc(x_b, rem_b)
        x_w = self.joint_enc(x_w, rem_w)
        x_b = self.dropout(x_b)
        x_w = self.dropout(x_w)

        T_b, T_w = x_b.size(1), x_w.size(1)
        causal_b = self._causal_mask(T_b, x_b.device)
        causal_w = self._causal_mask(T_w, x_w.device)

        x_b = self.self_attn_b(x_b, mask=causal_b)
        x_w = self.self_attn_w(x_w, mask=causal_w)

        x_review = self.cross_attn(x_b, x_w)

        if mask is not None:
            vec = self.pooling(x_review, mask=mask)
        else:
            vec = self.pooling(x_review)

        if env_vec is not None:
            env_feat = F.relu(self.env_proj(env_vec))
        else:
            env_feat = torch.zeros_like(vec)

        combined = torch.cat([vec, env_feat], dim=-1)
        combined = F.relu(self.combine(combined))
        return combined

    def predict_theta(self, x, env_vec, mask=None):
        combined = self._encode(x, env_vec, mask)
        return self.output_head.get_ordinal_logits(combined)

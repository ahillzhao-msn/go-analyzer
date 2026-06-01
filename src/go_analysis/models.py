"""
Go game data models and feature definitions.

Feature vector (12 dimensions per move):
  [0]  top1_hit        — 0/1, player's move == top KataGo move (merged accuracy + top1)
  [1]  top5_hit        — 0/1, player's move in top 5 KataGo moves
  [2]  complexity      — 1 - max(policy), how spread out the policy is
  [3]  policy_entropy  — float, entropy of full policy vector
  [4]  prior           — float, KataGo's prior probability for this move
  [5]  winrate         — float, KataGo's winrate estimate for current player
  [6]  score_lead      — float, KataGo's expected score lead
  [7]  score_stdev     — float, standard deviation of score estimate (uncertainty)
  [8]  utility         — float, KataGo's combined winrate+score utility
  [9]  lcb             — float, lower-confidence-bound score_lead
  [10] avg_visits      — float, visit ratio for this move (visits / total visits)
  [11] player          — 0=black, 1=white

Global stats (12 dimensions per game):
  [0]  top1_rate        — fraction of moves matching KataGo top1
  [1]  top5_rate
  [2]  avg_complexity
  [3]  avg_entropy
  [4]  avg_prior
  [5]  avg_winrate
  [6]  avg_score_lead
  [7]  avg_score_stdev
  [8]  avg_utility
  [9]  avg_lcb
  [10] avg_visits
  [11] move_count       — log-scaled number of moves in game
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GoMoveData:
    """Single move's analysis data and label."""
    move_num: int
    player: int          # 0=black, 1=white
    features: np.ndarray  # shape (12,)
    label: Optional[int] = None  # ordinal label 0..N-1 (e.g. 段位等级)

    FEATURE_NAMES = [
        "top1_hit", "top5_hit", "complexity", "policy_entropy",
        "prior", "winrate", "score_lead", "score_stdev",
        "utility", "lcb", "avg_visits", "player"
    ]


@dataclass
class GoGameData:
    """All moves from one game + global stats + label."""
    game_id: str
    moves: list = field(default_factory=list)  # list of GoMoveData
    global_stats: np.ndarray = field(default_factory=lambda: np.zeros(12, dtype=np.float32))
    label: Optional[int] = None


def extract_features_from_analysis(analysis: dict, player: str) -> list:
    """Extract per-move features from a single KataGo analysis JSON dict.

    Parameters
    ----------
    analysis : dict
        KataGo analysis result for one game.
        Expected shape: {
            "moveInfos": [{
                "move": "D4",
                "turnNumber": 1,
                "player": "B",
                "prior": 0.02,
                "policy": [...],
                "winrate": 0.55,
                "scoreLead": 3.2,
                "scoreStdev": 10.0,
                "lcb": 0.5,
                "utility": 0.6,
                "order": 0,
                "pv": [...]
            }, ...],
            "rootInfo": {...},
            "gameRating": {...}
        }
    player : str
        "B" or "W" — which side to extract features for.

    Returns
    -------
    list of GoMoveData
    """
    move_infos = analysis.get("moveInfos", [])
    moves_data = []
    player_id = 0 if player == "B" else 1

    # Count total visits across all moves for visit ratio computation
    total_visits = sum(mi.get("visits", 0) for mi in move_infos) or 1

    for mi in move_infos:
        if mi.get("player", "") != player:
            continue

        policy = mi.get("policy", [])
        order = mi.get("order", -1)

        # top1_hit (merged accuracy + top1)
        top1_hit = 1.0 if order == 0 else 0.0
        top5_hit = 1.0 if 0 <= order < 5 else 0.0

        # complexity: 1 - max(policy)
        policy_arr = np.array(policy, dtype=np.float32) if policy else np.array([1.0])
        max_p = policy_arr.max()
        complexity = 1.0 - max_p

        # policy entropy
        if policy and len(policy) > 1:
            p_arr = np.clip(policy_arr, 1e-10, 1.0)
            policy_entropy = -float(np.sum(p_arr * np.log(p_arr)))
        else:
            policy_entropy = 0.0

        prior = mi.get("prior", 0.0)
        winrate = mi.get("winrate", 0.5)
        score_lead = mi.get("scoreLead", 0.0)
        score_stdev = mi.get("scoreStdev", 0.0)
        utility = mi.get("utility", 0.0)
        lcb = mi.get("lcb", 0.0)
        avg_visits = mi.get("visits", 0) / total_visits

        feats = np.array([
            top1_hit, top5_hit, complexity, policy_entropy,
            prior, winrate, score_lead, score_stdev,
            utility, lcb, avg_visits,
            float(player_id),
        ], dtype=np.float32)

        md = GoMoveData(
            move_num=mi.get("turnNumber", 0),
            player=player_id,
            features=feats,
        )
        moves_data.append(md)

    return moves_data


def compute_global_stats(moves: list) -> np.ndarray:
    """Compute 12-dim global stats vector from a list of GoMoveData."""
    if not moves:
        return np.zeros(12, dtype=np.float32)

    feats = np.stack([m.features for m in moves], axis=0)

    top1_rate = float(feats[:, 0].mean())
    top5_rate = float(feats[:, 1].mean())
    avg_cmp = float(feats[:, 2].mean())
    avg_ent = float(feats[:, 3].mean())
    avg_prior = float(feats[:, 4].mean())
    avg_val = float(feats[:, 5].mean())
    avg_score = float(feats[:, 6].mean())
    avg_stdev = float(feats[:, 7].mean())
    avg_util = float(feats[:, 8].mean())
    avg_lcb = float(feats[:, 9].mean())
    avg_visits = float(feats[:, 10].mean())
    move_count = np.log10(max(len(moves), 1))

    return np.array([
        top1_rate, top5_rate, avg_cmp, avg_ent,
        avg_prior, avg_val, avg_score, avg_stdev,
        avg_util, avg_lcb, avg_visits,
        move_count,
    ], dtype=np.float32)

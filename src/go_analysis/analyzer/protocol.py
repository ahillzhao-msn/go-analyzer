"""
KataGo 分析协议封装。

处理:
  - SGF ↔ moves 数组转换
  - 查询构建 (适应 KataGo v1.13 ~ v1.16 的 API)
  - 响应解析 (提取 per-move 特征)
  - 坐标系统转换 (SGF ↔ GTP)
"""

import json
from typing import Optional

from ..sgf_parser import SGF, Move as SGMove


class AnalysisProtocol:
    """KataGo 分析协议的封装。

    提供与 KataGo 版本无关的查询构建和响应解析。
    """

    # KataGo 使用的 GTP 坐标 (跳过 I)
    GTP_COLS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"

    @staticmethod
    def sgf_to_moves(sgf_content: str) -> list:
        """将 SGF 内容解析为 moves 数组 (只取主线)."""
        tree = SGF.parse(sgf_content)
        moves = []
        node = tree.root
        while node and node.children:
            node = node.children[0]  # 取主线 (第一个分支)
            m = node.move
            if m and not m.is_pass:
                moves.append([m.player, m.gtp()])
        return moves

    @staticmethod
    def sgf_file_to_moves(sgf_path: str) -> list:
        """从 SGF 文件读取并解析为 moves 数组。"""
        tree = SGF.parse_file(sgf_path)
        moves = []
        for node in tree.nodes_in_tree:
            m = node.move
            if m and not m.is_pass:
                moves.append([m.player, m.gtp()])
        return moves

    @staticmethod
    def build_query(
        game_id: str,
        moves_history: list,
        visits: int = 50,
        rules: str = "chinese",
        komi: float = 7.5,
        board_size: int = 19,
    ) -> str:
        """为指定局面构建 KataGo 分析查询。

        Parameters
        ----------
        game_id : str
            查询标识符。
        moves_history : list
            [player, coord] 列表，如 [["B","Q16"], ["W","D4"]]。
        visits : int
            每手访问次数。
        rules : str
            规则 (chinese/japanese/korean)。
        komi : float
            贴目。
        board_size : int
            棋盘大小。

        Returns
        -------
        str
            序列化的 JSON 查询字符串。
        """
        query = {
            "id": game_id,
            "moves": moves_history,
            "maxVisits": visits,
            "rules": rules,
            "komi": komi,
            "boardXSize": board_size,
            "boardYSize": board_size,
            "includePolicy": True,
        }
        return json.dumps(query)

    @staticmethod
    def extract_player_move(
        move_infos: list,
        target_move: str,
    ) -> Optional[dict]:
        """在 KataGo 的候选落子列表中查找指定手。

        Parameters
        ----------
        move_infos : list
            KataGo 响应中的 moveInfos 列表。
        target_move : str
            要查找的落子 GTP 坐标 (如 "Q16")。

        Returns
        -------
        dict or None
            包含 order, prior, winrate, scoreLead, lcb 的字典。
            None 表示该手不在候选列表中 (很差的手)。
        """
        for m in move_infos:
            if m.get("move") == target_move:
                return {
                    "order": m.get("order", -1),
                    "prior": m.get("prior", 0.0),
                    "winrate": m.get("winrate", 0.5),
                    "score_lead": m.get("scoreLead", 0.0),
                    "score_stdev": m.get("scoreStdev", 0.0),
                    "utility": m.get("utility", 0.0),
                    "lcb": m.get("lcb", 0.0),
                    "visits": m.get("visits", 0),
                    "is_visited": True,
                }
        return None

    @staticmethod
    def extract_features_from_moves(
        all_moves: list,
        analysis_results: dict,
    ) -> list:
        """从多位置分析结果中提取每个玩家的 12 维特征。

        Parameters
        ----------
        all_moves : list
            完整的 [player, coord] 列表。
        analysis_results : dict
            {query_idx: parsed_response} 字典。

        Returns
        -------
        list of dict
            每项包含 player, move_num, features (12,), label。
        """
        from ..models import GoMoveData
        import numpy as np

        features = []
        for query_idx, response in analysis_results.items():
            if query_idx >= len(all_moves):
                continue

            player, move_coord = all_moves[query_idx]
            move_infos = response.get("moveInfos", [])
            player_id = 0 if player == "B" else 1

            info = AnalysisProtocol.extract_player_move(move_infos, move_coord)

            if info is None:
                # Not in top candidates — assign worst order
                info = {
                    "order": len(move_infos),  # one below worst shown
                    "prior": 0.0,
                    "winrate": 0.5,
                    "score_lead": 0.0,
                    "score_stdev": 0.0,
                    "utility": 0.0,
                    "lcb": 0.0,
                    "visits": 0,
                    "is_visited": False,
                }

            # Compute policy entropy (from root policy in response)
            root_info = response.get("rootInfo", {})
            policy = root_info.get("policy", [])
            if policy and len(policy) > 1:
                p_arr = np.clip(np.array(policy, dtype=np.float32), 1e-10, 1.0)
                policy_entropy = -float(np.sum(p_arr * np.log(p_arr)))
            else:
                policy_entropy = 0.0

            # Complexity: 1 - max(policy) from root
            max_p = max(policy) if policy else 1.0
            complexity = 1.0 - max_p

            # Total visits for ratio computation
            total_visits = sum(m.get("visits", 0) for m in move_infos) or 1

            # Player's visit ratio
            avg_visits = info["visits"] / total_visits

            feats = np.array([
                float(info["order"] == 0),   # top1_hit
                float(info["order"] < 5),    # top5_hit
                complexity,                   # complexity
                policy_entropy,               # policy_entropy
                info["prior"],                # prior
                info["winrate"],              # winrate
                info["score_lead"],           # score_lead
                info["score_stdev"],          # score_stdev
                info["utility"],              # utility
                info["lcb"],                  # lcb
                avg_visits,                   # avg_visits
                float(player_id),             # player
            ], dtype=np.float32)

            features.append({
                "player": player,
                "move_num": query_idx,
                "features": feats,
            })

        return features

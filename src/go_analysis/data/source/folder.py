"""FolderSource — 本地文件夹 SGF 来源。"""
import re
import os
from pathlib import Path
from typing import Optional

from .base import BaseSource


# SGF 文件头中提取段位信息的简单正则
RE_RANK = re.compile(r'BR\[([^\]]*)\]|WR\[([^\]]*)\]')
RE_PLAYER = re.compile(r'PB\[([^\]]*)\]|PW\[([^\]]*)\]')
RE_KOMI = re.compile(r'KM\[([^\]]*)\]')
RE_RESULT = re.compile(r'RE\[([^\]]*)\]')
RE_SZ = re.compile(r'SZ\[([^\]]*)\]')
RE_HA = re.compile(r'HA\[([^\]]*)\]')


def _parse_metadata(sgf_content: str, game_id: str, group: str = "") -> dict:
    """从 SGF 内容中提取元数据。不解析整个 SGF 树，只用正则扫描文件头。"""
    # 只取文件头前 2000 个字符 — 元数据在 (; ... 内
    header = sgf_content[:2000]

    meta = {
        "game_id": game_id,
        "group": group,
    }

    # 段位
    ranks = RE_RANK.findall(header)
    if ranks:
        for br, wr in ranks:
            if br:
                meta["black_rank"] = br
            if wr:
                meta["white_rank"] = wr

    # 玩家名
    players = RE_PLAYER.findall(header)
    if players:
        for pb, pw in players:
            if pb:
                meta["black_player"] = pb
            if pw:
                meta["white_player"] = pw

    # 贴目
    km = RE_KOMI.findall(header)
    if km:
        try:
            meta["komi"] = float(km[0])
        except ValueError:
            pass

    # 结果
    res = RE_RESULT.findall(header)
    if res:
        meta["result"] = res[0]

    # 棋盘大小
    sz = RE_SZ.findall(header)
    if sz:
        try:
            meta["board_size"] = int(sz[0])
        except ValueError:
            pass

    # 让子
    ha = RE_HA.findall(header)
    if ha:
        try:
            meta["handicap"] = int(ha[0])
        except ValueError:
            pass

    return meta


class FolderSource(BaseSource):
    """本地文件夹 SGF 来源。

    递归扫描目录下的所有 *.sgf 文件。
    目录结构: {root}/{group}/*.sgf  (group 如 "1d-3d", "pro" 等)
    """

    def __init__(self, root: str, encoding: str = "utf-8"):
        self.root = Path(root)
        self.encoding = encoding
        self._cache: Optional[list[tuple[str, str, str]]] = None  # (game_id, path, group)

    def _scan(self) -> list[tuple[str, str, str]]:
        """扫描所有 SGF 文件，返回 (game_id, path, group)。"""
        if self._cache is not None:
            return self._cache
        results = []
        for f in sorted(self.root.rglob("*.sgf")):
            game_id = f.stem
            # group = 父目录名 (如 "1d-3d")
            group = f.parent.name if f.parent != self.root else ""
            results.append((game_id, str(f), group))
        self._cache = results
        return results

    def list_games(self) -> list[str]:
        return [gid for gid, _, _ in self._scan()]

    def get_game(self, game_id: str) -> tuple[str, dict]:
        for gid, path_str, group in self._scan():
            if gid == game_id:
                content = Path(path_str).read_text(encoding=self.encoding, errors="replace")
                meta = _parse_metadata(content, game_id, group)
                return content, meta
        raise KeyError(f"Game not found: {game_id}")

    def count(self) -> int:
        return len(self._scan())

    def exists(self, game_id: str) -> bool:
        return any(gid == game_id for gid, _, _ in self._scan())

    def get_group(self, game_id: str) -> str:
        """返回游戏所属的组名。"""
        for gid, _, group in self._scan():
            if gid == game_id:
                return group
        return ""

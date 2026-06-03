"""
SGF 预验证 — 在送 KataGo 分析前检测非法落子。

分析流程:
1. 解析 SGF 为 move 序列
2. 模拟下棋, 检测落子是否在已有子上
3. 标记有非法步的棋谱, 跳过分析

用法::

    from go_analysis.sgf_validator import validate_sgf, filter_valid_sgfs

    bad = validate_sgf("game.sgf")  # 返回 None 表示合法
    valid, bad = filter_valid_sgfs("./sgf_dir/")
"""

from pathlib import Path
from typing import Optional
from .sgf_parser import SGF


def validate_sgf(sgf_path: str | Path) -> Optional[str]:
    """
    验证 SGF 文件是否有非法落子.

    Returns
    -------
    str or None
        如果有非法步, 返回错误描述。合法返回 None.
    """
    try:
        tree = SGF.parse_file(str(sgf_path))
    except Exception as e:
        return f"ParseError: {e}"

    moves = []
    for node in tree.nodes_in_tree:
        m = node.move
        if m and not m.is_pass and m.coords is not None:
            moves.append(m)

    if not moves:
        return "No moves found"

    # 模拟棋盘
    board = {}
    for move in moves:
        coord = move.coords
        key = (coord[0], coord[1])
        if key in board:
            return f"Illegal move at turn {len(board)+1}: {move.gtp()} (already occupied by {board[key]})"
        board[key] = move.player

    return None


def filter_valid_sgfs(sgf_dir: str | Path) -> tuple[list[Path], list[tuple[Path, str]]]:
    """
    批量验证目录下所有 SGF, 返回 (合法列表, [(非法文件, 原因)])。
    """
    sgf_dir = Path(sgf_dir)
    valid = []
    invalid = []

    for f in sorted(sgf_dir.glob("*.sgf")):
        err = validate_sgf(f)
        if err:
            invalid.append((f, err))
        else:
            valid.append(f)

    return valid, invalid


def clean_invalid_sgfs(sgf_dir: str | Path, dry_run: bool = True) -> int:
    """
    将目录下所有非法 SGF 移到 .bad/ 子目录。

    Returns
    -------
    int
        移除了多少个文件。
    """
    sgf_dir = Path(sgf_dir)
    _, invalid = filter_valid_sgfs(sgf_dir)

    if not invalid:
        return 0

    bad_dir = sgf_dir / ".bad"
    if not dry_run:
        bad_dir.mkdir(exist_ok=True)

    for f, reason in invalid:
        if dry_run:
            print(f"  WOULD MOVE: {f.name}: {reason}")
        else:
            dest = bad_dir / f.name
            f.rename(dest)
            print(f"  MOVED: {f.name}: {reason}")

    return len(invalid)

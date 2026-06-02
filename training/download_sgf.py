#!/usr/bin/env python3
"""
OGS 棋谱下载 — 正态分布分层采样
通过 OGS 玩家关系图 BFS，按等级分组下载 SGF。

使用方式:
  python3 training/download_sgf.py

认证: 依赖 /tmp/ogs_cookies.txt (已通过 curl 登录创建)
"""

import json
import os
import sys
import time
import pickle
import random
import subprocess
from pathlib import Path

# ── 配置 ─────────────────────────────────────────────
API = "https://online-go.com/api/v1"
COOKIE_FILE = "/tmp/ogs_cookies.txt"
PROGRESS_FILE = Path(__file__).parent / ".download_progress.pkl"
OUTPUT_DIR = Path(__file__).parent

# 正态分布分组 (ranking 映射到段位)
RANK_GROUPS = [
    ("30k-10k", 0, 20, 800),
    ("9k-1k",   20, 30, 1600),
    ("1d-3d",   30, 32, 2400),
    ("4d-6d",   33, 35, 1600),
    ("7d-9d",   36, 38, 800),
    ("pro",     39, 99, 800),
]

# 质量过滤
MIN_MOVES = 50
MAX_MOVES = 500
REQ_DELAY = 0.5   # 秒
BATCH_SIZE = 30   # 每批报告一次

# BFS 配置
SEED_PLAYERS = [1052589]  # ZebraBob
MAX_PLAYER_GAMES = 100
MAX_GAMES_PER_PAGE = 50


# ── curl 封装 ────────────────────────────────────────
def curl_get(path, params=None, raw_path=False, method="GET", data=None):
    """封装 curl GET/POST，返回 dict 或 bytes"""
    if raw_path:
        url = path
    else:
        url = f"{API}{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"

    cmd = ["curl", "-s", "-b", COOKIE_FILE, "-w", "\n%{http_code}"]
    if data:
        cmd += ["-X", method, "-H", "Content-Type: application/json",
                "-d", json.dumps(data)]
    cmd.append(url)

    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            lines = result.stdout.strip().rsplit("\n", 1)
            if len(lines) < 2:
                return None
            body, code = lines[0], lines[1].strip()
            code = int(code)
            if code == 429:
                wait = 5 * (attempt + 1)
                print(f"  429 速率限制，等待 {wait}s...")
                time.sleep(wait)
                continue
            if code == 404:
                return None
            if code >= 400:
                if attempt < 2:
                    time.sleep(1)
                    continue
                return None
            return json.loads(body) if body else None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as e:
            if attempt < 2:
                time.sleep(1)
                continue
            return None
    return None


def curl_sgf(game_id, filepath):
    """下载 SGF 到文件"""
    if os.path.exists(filepath):
        return True
    url = f"{API}/games/{game_id}/sgf"
    cmd = ["curl", "-s", "-b", COOKIE_FILE, "-o", filepath, "-w", "%{http_code}", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        code = result.stdout.strip()
        return code == "200"
    except Exception:
        return False


# ── 段位工具 ─────────────────────────────────────────
def rank_to_group(rank_val):
    if rank_val is None:
        return None
    for group_name, rmin, rmax, _ in RANK_GROUPS:
        if rmin <= rank_val < rmax:
            return group_name
    return None


def game_rank(game):
    """从 game 详情中提取最高段位"""
    gd = game.get('gamedata') or {}
    players = gd.get('players', {})
    ranks = []
    for color in ('black', 'white'):
        p = players.get(color, {})
        r = p.get('rank')
        if r is not None:
            ranks.append(r)
    if not ranks:
        return None
    return max(ranks)


def quality_filter(game):
    """检查对局质量"""
    gd = game.get('gamedata') or {}

    # 棋盘尺寸
    w = gd.get('width') or game.get('width', 0)
    h = gd.get('height') or game.get('height', 0)
    if w != 19 or h != 19:
        return False

    # 让子
    hcap = gd.get('handicap') or game.get('handicap', 0)
    if hcap and hcap > 0:
        return False

    # 是否排名对局
    if not (gd.get('ranked') or game.get('ranked')):
        return False

    # 手数
    moves = gd.get('moves', [])
    if len(moves) < MIN_MOVES or len(moves) > MAX_MOVES:
        return False

    # bot 检测
    players_data = game.get('players', {})
    for color in ('black', 'white'):
        p = players_data.get(color, {})
        uc = str(p.get('ui_class', ''))
        if 'bot' in uc.lower():
            return False
        uname = str(p.get('username', ''))
        if any(b in uname.lower() for b in ['bot', 'ai-', '[bot]']):
            return False

    return True


# ── 核心采集 ─────────────────────────────────────────
def get_player_games(player_id, max_games=MAX_PLAYER_GAMES):
    """获取某玩家的对局列表"""
    all_games = []
    page = 1
    while len(all_games) < max_games:
        data = curl_get(f"/players/{player_id}/games",
                        {"page_size": MAX_GAMES_PER_PAGE, "page": page})
        if not data or not data.get('results'):
            break
        all_games.extend(data['results'])
        if not data.get('next'):
            break
        page += 1
        time.sleep(REQ_DELAY)
    return all_games[:max_games]


def main():
    print("=" * 60)
    print(" OGS 棋谱下载 — 正态分布分层采样")
    print("=" * 60)

    # 创建目录
    for group_name, _, _, _ in RANK_GROUPS:
        (OUTPUT_DIR / group_name).mkdir(parents=True, exist_ok=True)

    target_total = sum(t for _, _, _, t in RANK_GROUPS)
    print(f"\n目标: {target_total} 盘")
    for g, _, _, t in RANK_GROUPS:
        print(f"  {g:>10s}: {t:>5d}")
    print()

    # 验证 cookie
    test = curl_get("/me/games", {"page_size": 1})
    if test is None:
        print("❌ 无法访问 API，请先运行: curl -c /tmp/ogs_cookies.txt -X POST "
              "https://online-go.com/api/v0/login/ -H 'Content-Type: application/json' "
              "-d '{\"username\":\"Zebrabob\",\"password\":\"53005075\"}'")
        sys.exit(1)

    print("✅ 认证正常")

    # 加载进度
    counts = {g: 0 for g, _, _, _ in RANK_GROUPS}
    downloaded_ids = set()
    player_queue = set(SEED_PLAYERS)
    visited_players = set()

    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, 'rb') as f:
                saved = pickle.load(f)
                counts.update(saved.get('counts', {}))
                downloaded_ids = saved.get('downloaded_ids', set())
                player_queue = saved.get('player_queue', player_queue)
                visited_players = saved.get('visited_players', set())
            print(f"✅ 恢复进度: 已下载 {len(downloaded_ids)} 盘, "
                  f"队列 {len(player_queue)} 玩家, "
                  f"已访问 {len(visited_players)} 玩家")
        except Exception:
            print("进度文件损坏，从头开始")
    else:
        print("从头开始")

    # 统计
    total_downloaded = sum(counts.values())
    print(f"\n当前: {total_downloaded}/{target_total} 盘")

    # ── BFS 主循环 ─────────────────────────────────
    attempts = 0
    no_new_games_count = 0
    stale_queue_count = 0

    while total_downloaded < target_total and player_queue:
        attempts += 1
        player_id = player_queue.pop()
        if player_id in visited_players:
            continue
        visited_players.add(player_id)

        # 获取该玩家对局
        games = get_player_games(player_id)
        if not games:
            stale_queue_count += 1
            continue
        stale_queue_count = 0

        needed = {g for g, _, _, t in RANK_GROUPS if counts[g] < t}
        if not needed:
            break

        # 从列表预筛
        candidate_games = []
        for g in games:
            gid = g.get('id')
            if not gid or gid in downloaded_ids:
                continue
            if not g.get('ranked'):
                continue
            if g.get('handicap', 0) > 0:
                continue
            if g.get('width') != 19 or g.get('height') != 19:
                continue
            # 检查 bot
            players = g.get('players', {})
            is_bot = False
            for color in ('black', 'white'):
                p = players.get(color, {})
                uc = str(p.get('ui_class', ''))
                if 'bot' in uc.lower():
                    is_bot = True
                    break
            if is_bot:
                continue
            candidate_games.append(gid)

        if not candidate_games:
            continue

        # 获取详情并下载
        batch_new = 0
        for gid in candidate_games:
            if gid in downloaded_ids:
                continue
            if not needed:
                break

            detail = curl_get(f"/games/{gid}")
            time.sleep(REQ_DELAY)
            if not detail:
                continue

            if not quality_filter(detail):
                continue

            r = game_rank(detail)
            if r is None:
                continue

            group = rank_to_group(r)
            if group is None or group not in needed:
                continue

            # 下载
            fpath = str(OUTPUT_DIR / group / f"{gid}.sgf")
            if curl_sgf(gid, fpath):
                downloaded_ids.add(gid)
                counts[group] += 1
                batch_new += 1

                # 对手入队
                for color in ('black', 'white'):
                    pid = detail.get(color)
                    if pid and pid not in visited_players and pid != player_id:
                        player_queue.add(pid)

                needed = {g for g, _, _, t in RANK_GROUPS if counts[g] < t}
                time.sleep(REQ_DELAY)

        total_downloaded = sum(counts.values())
        pct = total_downloaded / target_total * 100

        if batch_new or attempts % 5 == 0:
            status = "  ".join(
                f"{g}:{counts[g]:>3d}/{t}" if counts[g] < t
                else f"{g}:{counts[g]:>3d}✓"
                for g, _, _, t in RANK_GROUPS
            )
            print(f"  [{attempts:>4d}] +{batch_new:>2d}  "
                  f"{total_downloaded:>4d}/{target_total} ({pct:>4.1f}%)  "
                  f"队列:{len(player_queue):>3d}  |  {status}")

        if batch_new == 0:
            no_new_games_count += 1
        else:
            no_new_games_count = 0

        # 队列枯竭处理：从已访问玩家中重新取
        if no_new_games_count >= 20 and len(player_queue) < 20:
            extra = random.sample(list(visited_players), min(30, len(visited_players)))
            player_queue.update(extra)
            no_new_games_count = 0
            print(f"  🔄 队列补充: +{len(extra)} 个已访问玩家")

        # 定期保存
        if attempts % 20 == 0:
            with open(PROGRESS_FILE, 'wb') as f:
                pickle.dump({
                    'counts': counts,
                    'downloaded_ids': downloaded_ids,
                    'player_queue': player_queue,
                    'visited_players': visited_players,
                }, f)

    # ── 最终保存 ────────────────────────────────────
    with open(PROGRESS_FILE, 'wb') as f:
        pickle.dump({
            'counts': counts,
            'downloaded_ids': downloaded_ids,
            'player_queue': player_queue,
            'visited_players': visited_players,
        }, f)

    print(f"\n{'='*60}")
    print(f" 完成!  访问玩家: {len(visited_players)}  |  总下载: {len(downloaded_ids)} 盘")
    print(f"{'='*60}")
    print(f"{'分组':>10s}  {'目标':>6s}  {'已下载':>6s}  {'完成率':>6s}")
    print("-" * 35)
    for g, _, _, t in RANK_GROUPS:
        c = counts.get(g, 0)
        pct = c / t * 100 if t else 0
        mark = " ✓" if c >= t else ""
        print(f"{g:>10s}  {t:>5d}  {c:>5d}  {pct:>5.0f}%{mark}")
    print("-" * 35)
    print(f"{'总计':>10s}  {target_total:>5d}  {total_downloaded:>5d}  "
          f"{total_downloaded/target_total*100:>5.0f}%")

    # 目录统计
    print(f"\n输出目录: {OUTPUT_DIR}")
    for g, _, _, _ in RANK_GROUPS:
        d = OUTPUT_DIR / g
        files = list(d.glob("*.sgf"))
        size = sum(f.stat().st_size for f in files) / 1024
        print(f"  {g}: {len(files)} 文件 ({size:.0f} KB)")


if __name__ == '__main__':
    main()

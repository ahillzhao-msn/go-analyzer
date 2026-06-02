#!/usr/bin/env python3
"""
GTL 棋谱下载 & 筛选 — 正态分布分层采样

来源: https://gtl.xmp.net/reviews/zip
10000 盘职业棋手 review 过的对局，按段位分组下载。

用法:
  python3 training/gtl_download.py

输出:
  training/{30k-10k, 9k-1k, 1d-3d, 4d-6d, 7d-9d, pro}/
"""

import json
import os
import sys
import re
import io
import zipfile
import urllib.request
import time
from pathlib import Path

# ── 配置 ─────────────────────────────────────────────
BASE_URL = "https://gtl.xmp.net/sgf/zip"
INDEX_URL = f"{BASE_URL}/INDEX.txt"
OUTPUT_DIR = Path(__file__).parent
TEMP_DIR = OUTPUT_DIR / ".tmp"

# 正态分布分组
RANK_GROUPS = [
    ("30k-10k",  800,  lambda r: r and r.endswith("k") and 10 <= int(r[:-1]) <= 30),
    ("9k-1k",   1600,  lambda r: r and r.endswith("k") and 1 <= int(r[:-1]) <= 9),
    ("1d-3d",   2400,  lambda r: r and r.endswith("d") and 1 <= int(r[:-1]) <= 3),
    ("4d-6d",   1600,  lambda r: r and r.endswith("d") and 4 <= int(r[:-1]) <= 6),
    ("7d-9d",    800,  lambda r: r and r.endswith("d") and 7 <= int(r[:-1]) <= 9),
    ("pro",      800,  lambda r: r == "pro"),
]

# Zip 文件列表
ZIP_FILES = [
    "001-999-reviews.zip",
    "1000-1999-reviews.zip",
    "2000-2999-reviews.zip",
    "3000-3999-reviews.zip",
    "4000-4999-reviews.zip",
    "5000-5999-reviews.zip",
    "6000-6999-reviews.zip",
    "7000-7999-reviews.zip",
    "8000-8999-reviews.zip",
    "9000-10000-reviews.zip",
]

# ── 段位解析 ─────────────────────────────────────────
def parse_rank(s):
    """解析段位字符串 → (数值, 类型) 或 None"""
    s = s.strip()
    if not s:
        return None
    if s == "pro":
        return (0, "pro")
    m = re.match(r'^(\d+)([dkp])$', s)
    if m:
        num = int(m.group(1))
        kind = m.group(2)
        if kind == 'd':
            return (num, 'dan')
        elif kind == 'k':
            return (num, 'kyu')
        elif kind == 'p':
            return (num, 'pro')
    return None


def rank_sort_key(rank_info):
    """排序用 key: 数值越小越强"""
    if rank_info is None:
        return 999
    val, kind = rank_info
    if kind == 'pro':
        return -val - 100  # pro always strongest
    elif kind == 'dan':
        return -val  # 9d=-9, 1d=-1
    else:  # kyu
        return val   # 1k=1, 30k=30


def stronger_rank(r1, r2):
    """取两者中较强的段位"""
    if r1 is None and r2 is None:
        return None
    if r1 is None:
        return r2
    if r2 is None:
        return r1
    return r1 if rank_sort_key(r1) < rank_sort_key(r2) else r2


def rank_to_label(rank_info):
    """段位信息 → 可读标签"""
    if rank_info is None:
        return "?"
    val, kind = rank_info
    if kind == 'pro':
        return f"{val}p"
    elif kind == 'dan':
        return f"{val}d"
    else:
        return f"{val}k"


def match_group(rank_info):
    """分配段位到分组"""
    if rank_info is None:
        return None
    label = rank_to_label(rank_info)
    for group_name, target, matcher in RANK_GROUPS:
        if matcher(label):
            return group_name
    return None


# ── INDEX 解析 ───────────────────────────────────────
def download_index():
    """下载并解析 INDEX.txt"""
    print(f"下载 INDEX.txt...")
    try:
        with urllib.request.urlopen(INDEX_URL, timeout=30) as resp:
            content = resp.read().decode()
    except Exception as e:
        print(f"  下载失败: {e}")
        sys.exit(1)

    lines = [l.rstrip() for l in content.split('\n')]

    # 跳过头部
    data_start = 0
    for i, l in enumerate(lines):
        if l.startswith('---'):
            data_start = i + 1
            break

    entries = []
    for line in lines[data_start:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 6:
            continue

        filename = parts[0]  # e.g. 5265-yly-TheCaptain-Vegetarian

        # 从尾部解析
        date = parts[-1]
        result = None
        komi_idx = -2

        # 检查倒数第二个字段是否是结果
        if not (parts[-2].startswith('20') and len(parts[-2]) == 10):
            result = parts[-2]
            komi_idx = -3

        komi = parts[komi_idx]
        is_handicap = bool(re.match(r'^h\d+$', komi))
        if is_handicap:
            continue  # 跳过让子棋

        # 解析玩家段位：parts[1]~parts[2] 是评审段位
        # 评审段位: parts[1]=数字, parts[2]=类型(pro/dan/kyu)
        # 之后到 komi 之前都是玩家段位
        reviewer_num = parts[1]
        reviewer_type = parts[2]
        reviewer_rank = f"{reviewer_num}{reviewer_type}" if reviewer_type in ('pro', 'dan', 'kyu') else None

        # 玩家段位在 parts[3] 到 komi_idx-1 之间
        player_ranks_raw = parts[3:komi_idx]
        player_ranks = []
        for r in player_ranks_raw:
            r = r.strip()
            parsed = parse_rank(r)
            if parsed:
                player_ranks.append(parsed)

        if not player_ranks:
            continue  # 无玩家段位信息，跳过

        # 取较强者的段位代表该局水平
        game_rank = None
        for pr in player_ranks:
            game_rank = stronger_rank(game_rank, pr)
        game_rank_label = rank_to_label(game_rank)

        # 分配分组
        group = match_group(game_rank)
        if group is None:
            continue

        # 确定 zip 文件
        match_id = re.match(r'^(\d+)', filename)
        if not match_id:
            continue
        game_num = int(match_id.group(1))

        for zf in ZIP_FILES:
            m = re.match(r'(\d+)-(\d+)', zf)
            if m:
                z_start = int(m.group(1))
                z_end = int(m.group(2))
                if z_start <= game_num <= z_end:
                    zip_file = zf
                    break
        else:
            continue

        entries.append({
            'filename': filename,
            'game_num': game_num,
            'rank': game_rank_label,
            'rank_info': game_rank,
            'group': group,
            'zip_file': zip_file,
            'players': player_ranks_raw,
        })

    return entries


# ── 采样 ─────────────────────────────────────────────
def sample_entries(entries):
    """按正态分布目标数量采样"""
    from collections import defaultdict

    # 按分组 group by
    by_group = defaultdict(list)
    for e in entries:
        by_group[e['group']].append(e)

    sampled = []
    for group_name, target, _ in RANK_GROUPS:
        pool = by_group.get(group_name, [])
        # 按游戏编号排序确保确定性的采样
        pool.sort(key=lambda e: e['game_num'])
        # 如果 pool 比目标多，均匀采样
        take = min(target, len(pool))
        if len(pool) > target:
            step = len(pool) / target
            indices = [int(i * step) for i in range(target)]
            chosen = [pool[i] for i in indices]
        else:
            chosen = pool
        sampled.extend(chosen)
        print(f"  {group_name:>10s}: {len(chosen):>4d}/{target:>4d} "
              f"(可用 {len(pool):>4d}){' ✓' if len(chosen) >= target else ' ⚠'}")
        if len(chosen) < target:
            print(f"    ⚠ 不足 {target - len(chosen)} 盘")

    # 按 zip 文件分组 (批量下载)
    by_zip = defaultdict(list)
    for e in sampled:
        by_zip[e['zip_file']].append(e)

    return sampled, dict(by_zip)


# ── SGF 下载 ─────────────────────────────────────────
def extract_sgf_from_zip(zip_url, filenames_needed, output_dir):
    """下载 zip 并仅提取需要的 SGF"""
    zf_name = zip_url.split('/')[-1]
    print(f"\n  下载 {zf_name}...")
    try:
        with urllib.request.urlopen(zip_url, timeout=120) as resp:
            data = resp.read()
    except Exception as e:
        print(f"    下载失败: {e}")
        return 0

    count = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if not name.endswith('.sgf'):
                    continue
                # 从 SGF 文件名中提取 review ID
                base = Path(name).stem
                if base not in filenames_needed:
                    continue
                entry = filenames_needed[base]
                group_dir = output_dir / entry['group']
                group_dir.mkdir(parents=True, exist_ok=True)

                target_path = group_dir / f"{base}.sgf"
                if target_path.exists():
                    count += 1
                    continue

                try:
                    content = zf.read(name)
                    with open(target_path, 'wb') as f:
                        f.write(content)
                    count += 1
                except Exception as e:
                    print(f"    提取 {base}.sgf 失败: {e}")
    except zipfile.BadZipFile as e:
        print(f"    压缩包损坏: {e}")
        return 0

    return count


def main():
    print("=" * 60)
    print(" GTL 棋谱下载 — 正态分布分层采样")
    print("=" * 60)

    # 创建目录
    for group_name, _, _ in RANK_GROUPS:
        (OUTPUT_DIR / group_name).mkdir(parents=True, exist_ok=True)

    total_target = sum(t for _, t, _ in RANK_GROUPS)
    print(f"\n目标: {total_target} 盘")
    for g, t, _ in RANK_GROUPS:
        print(f"  {g:>10s}: {t:>5d}")

    # Step 1: 下载并解析 INDEX
    print(f"\n[1/4] 解析 INDEX.txt...")
    entries = download_index()
    print(f"  非让子棋: {len(entries)} 局")

    # Step 2: 采样
    print(f"\n[2/4] 正态分布采样...")
    sampled, by_zip = sample_entries(entries)
    total_sampled = len(sampled)
    print(f"\n  总计采样: {total_sampled}/{total_target} 局")

    # Step 3: 下载 SGF
    print(f"\n[3/4] 下载 SGF 文件...")
    print(f"  涉及 {len(by_zip)} 个 zip 文件")

    # 构建 filename → entry 映射
    needed_map = {e['filename']: e for e in sampled}

    total_downloaded = 0
    for zip_name in sorted(by_zip.keys()):
        zip_url = f"{BASE_URL}/{zip_name}"
        count = extract_sgf_from_zip(zip_url, needed_map, OUTPUT_DIR)
        total_downloaded += count
        print(f"    → 已提取 {count} 个 SGF (累计 {total_downloaded}/{total_sampled})")

    # Step 4: 验证
    print(f"\n[4/4] 验证结果...")
    print()
    print(f"{'分组':>10s}  {'目标':>6s}  {'已下载':>6s}  {'完成率':>6s}")
    print("-" * 35)
    for group_name, target, _ in RANK_GROUPS:
        d = OUTPUT_DIR / group_name
        files = list(d.glob("*.sgf"))
        actual = len(files)
        pct = actual / target * 100 if target else 0
        mark = " ✓" if actual >= target else ""
        print(f"{group_name:>10s}  {target:>5d}  {actual:>5d}  {pct:>5.0f}%{mark}")

    total_actual = sum(len(list((OUTPUT_DIR / g).glob("*.sgf"))) for g, _, _ in RANK_GROUPS)
    total_target = sum(t for _, t, _ in RANK_GROUPS)
    print("-" * 35)
    print(f"{'总计':>10s}  {total_target:>5d}  {total_actual:>5d}  "
          f"{total_actual/total_target*100:>5.0f}%")

    # 文件大小
    print(f"\n输出目录: {OUTPUT_DIR}")
    total_size = 0
    for group_name, _, _ in RANK_GROUPS:
        d = OUTPUT_DIR / group_name
        files = list(d.glob("*.sgf"))
        size = sum(f.stat().st_size for f in files) / 1024
        total_size += size
        print(f"  {group_name}: {len(files)} 文件 ({size:.0f} KB)")
    print(f"  总计: {total_size:.0f} KB ({total_size/1024:.1f} MB)")


if __name__ == '__main__':
    main()

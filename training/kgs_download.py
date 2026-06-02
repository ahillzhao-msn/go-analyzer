#!/usr/bin/env python3
"""
KGS Archive 下载 & 筛选 — 双源: 7d+ 归档 + 4d+ 归档

来源:
  https://u-go.net/gamerecords/  (7d+)
  https://u-go.net/gamerecords-4d/ (4d+)

用法:
  python3 training/kgs_download.py
"""

import os
import re
import sys
import io
import tarfile
import zipfile
import random
import urllib.request
import time
from pathlib import Path

random.seed(42)

OUTPUT_DIR = Path(__file__).parent
BASE_7D = "https://u-go.net/gamerecords/"
DL_7D = "https://dl.u-go.net/gamerecords"
BASE_4D = "https://u-go.net/gamerecords-4d/"
DL_4D = "https://dl.u-go.net/gamerecords-4d"

MIN_MOVES = 250
MAX_SCORE_DIFF = 20


def get_links(url, dl_prefix, suffix_filter=None):
    """从页面获取下载链接"""
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode()
    links = re.findall(rf'href="({re.escape(dl_prefix)}/[^"]+)"', html)
    if suffix_filter:
        links = [l for l in links if l.endswith(suffix_filter)]
    return sorted(set(links))


def check_sgf(text):
    m = re.search(r'SZ\[(\d+)\]', text)
    if m and int(m.group(1)) != 19: return False
    m = re.search(r'HA\[(\d+)\]', text)
    if m and int(m.group(1)) > 0: return False
    moves = len(re.findall(r'[BW]\[', text))
    if moves < MIN_MOVES: return False
    m = re.search(r'RE\[([^]]+)\]', text)
    if m:
        sm = re.match(r'[BW]\+([\d.]+)', m.group(1))
        if sm and float(sm.group(1)) >= MAX_SCORE_DIFF: return False
    return True


def parse_rank(text):
    """从 SGF 中提取玩家段位"""
    for field in ['BR', 'WR']:
        m = re.search(rf'{field}\[(\d+[dkp])\]', text)
        if m: return m.group(1)
    for field in ['PB', 'PW']:
        m = re.search(rf'{field}\[[^\]]*?\((\d+[dkp])\)\]', text)
        if m: return m.group(1)
    m = re.search(r'C\[.*?(?:rank|Rank)\s*:\s*(\d+[dkp])', text)
    if m: return m.group(1)
    return None


def rank_to_group(r):
    if not r: return None
    m = re.match(r'(\d+)([dkp])', r)
    if not m: return None
    n, k = int(m.group(1)), m.group(2)
    if k == 'p': return 'pro'
    elif k == 'd':
        return '1d-3d' if n <= 3 else '4d-6d' if n <= 6 else '7d-9d'
    else:
        return '30k-10k' if n >= 10 else '9k-1k'


def process_archive(url, source_label):
    """下载并处理一个归档文件 (支持 .zip 和 .tar.gz)"""
    fname = url.split('/')[-1]
    print(f"\n  [{fname}]...", end="", flush=True)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
        print(f" {len(data)/1024:.0f} KB", flush=True)
    except Exception as e:
        print(f" ✗ {e}", flush=True)
        return 0

    batch_new = 0
    sgf_data = []

    try:
        if fname.endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                sgf_data = [(n, zf.read(n)) for n in zf.namelist() if n.endswith('.sgf')]
        elif fname.endswith('.tar.gz'):
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tf:
                sgf_data = [(m.name, tf.extractfile(m).read())
                           for m in tf.getmembers()
                           if m.name.endswith('.sgf') and m.isfile()]
    except Exception as e:
        print(f"    ✗ 解压失败: {e}")
        return 0

    random.shuffle(sgf_data)

    for sgf_name, content in sgf_data:
        try:
            text = content.decode('utf-8', errors='replace')
        except:
            continue
        if not check_sgf(text):
            continue

        rank = parse_rank(text)
        group = rank_to_group(rank)
        if not group:
            continue

        # Check if group still needs games
        d = OUTPUT_DIR / group
        existing = len(list(d.glob("*.sgf"))) if d.exists() else 0
        target = {"pro": 800, "7d-9d": 800, "4d-6d": 1600, "1d-3d": 2400,
                  "9k-1k": 1600, "30k-10k": 800}.get(group, 0)
        if existing >= target:
            continue

        d.mkdir(parents=True, exist_ok=True)
        out = d / f"{source_label}_{Path(sgf_name).stem}.sgf"
        if out.exists():
            continue
        with open(out, 'wb') as f:
            f.write(content)
        batch_new += 1

        if batch_new >= 200:  # 每包最多取 200 盘 (足够填缺口)
            break

    if batch_new:
        print(f"    +{batch_new}", flush=True)
    return batch_new


def main():
    print("=" * 60)
    print(" KGS Archive 双源下载 — 4d+ & 7d+")
    print("=" * 60)

    # 获取链接
    print("\n[1/3] 获取 KGS 归档列表...")
    links_7d = get_links(BASE_7D, DL_7D, '.zip')
    links_4d = get_links(BASE_4D, DL_4D, '.tar.gz')
    print(f"  7d+ (2001-2019): {len(links_7d)} zip")
    print(f"  4d+ (2007-2019): {len(links_4d)} tar.gz")

    # 去除 4d+ 中已处理的年度归档(太大, 只处理月度)
    links_4d_monthly = [l for l in links_4d if re.search(r'\d{4}_\d{2}', l)]
    links_4d_yearly = [l for l in links_4d if l not in links_4d_monthly]
    print(f"  4d+ monthly: {len(links_4d_monthly)}")
    print(f"  4d+ yearly:  {len(links_4d_yearly)} (跳过, 太大了)")

    # 统计已有
    existing = {}
    for g in ["pro", "7d-9d", "4d-6d", "1d-3d", "9k-1k", "30k-10k"]:
        d = OUTPUT_DIR / g
        existing[g] = len(list(d.glob("*.sgf"))) if d.exists() else 0
    total_existing = sum(existing.values())
    total_target = 8000
    print(f"\n  已入库: {total_existing}/{total_target}")

    # 处理
    print(f"\n[2/3] 处理 7d+ (已处理过, 只查新缺口)...")
    total_new = 0
    for url in links_7d:
        total_new += process_archive(url, 'kgs7')
        if total_new > 500:  # 7d+ 已经差不多了
            break

    print(f"\n[3/3] 处理 4d+ (月度包, 填 1d-3d)...")
    for url in links_4d_monthly:
        total_new += process_archive(url, 'kgs4')
        if total_new > 3000:  # 足够了
            break

    # 统计
    print(f"\n{'='*60}")
    print(" 完成")
    print(f"{'='*60}")
    print(f"{'分组':>10s}  {'目标':>6s}  {'已下载':>6s}  {'完成率':>6s}")
    print("-" * 35)
    for g, t in [("30k-10k",800),("9k-1k",1600),("1d-3d",2400),
                 ("4d-6d",1600),("7d-9d",800),("pro",800)]:
        d = OUTPUT_DIR / g
        c = len(list(d.glob("*.sgf"))) if d.exists() else 0
        pct = c/t*100 if t else 0
        mark = " ✓" if c >= t else ""
        print(f"{g:>10s}  {t:>5d}  {c:>5d}  {pct:>5.0f}%{mark}")
    total_c = 0
    for g, t in [("30k-10k",800),("9k-1k",1600),("1d-3d",2400),
                 ("4d-6d",1600),("7d-9d",800),("pro",800)]:
        d = OUTPUT_DIR / g
        c = len(list(d.glob("*.sgf"))) if d.exists() else 0
        total_c += c


if __name__ == '__main__':
    main()

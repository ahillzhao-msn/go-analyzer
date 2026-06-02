#!/usr/bin/env python3
"""
三源合一下载脚本：go-dataset + GTL + OGS

1. go-dataset (featurecat/go-dataset): 主力填充 1d-9p
2. GTL (已有): kyu 级基底
3. OGS API: 少量掺杂增加随机性

质量过滤:
  - 手数 > 250
  - 无让子 (HA[0] 或 HA 字段缺失)
  - 水平相当: 胜负差距 < 10 子
  - 19×19 棋盘

用法:
  python3 training/unified_download.py
"""

import json
import os
import re
import sys
import random
import subprocess
import shutil
import urllib.request
import time
from pathlib import Path

random.seed(42)

# ── 配置 ─────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).parent
TEMP_DIR = OUTPUT_DIR / ".tmp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

GO_DATASET_BASE = "https://raw.githubusercontent.com/featurecat/go-dataset/master"
API_GH = "https://api.github.com/repos/featurecat/go-dataset/contents"

RANK_TARGETS = {
    "30k-10k": 800, "9k-1k": 1600, "1d-3d": 2400,
    "4d-6d": 1600, "7d-9d": 800, "pro": 800,
}

# go-dataset rank → 段位组
RANK_DIR_MAP = {
    "1d": "1d-3d", "2d": "1d-3d", "3d": "1d-3d",
    "4d": "4d-6d", "5d": "4d-6d", "6d": "4d-6d",
    "7d": "7d-9d", "8d": "7d-9d", "9d": "7d-9d",
    "Pro": "pro",
    # 低段位少量混入
    "15k": "9k-1k", "14k": "9k-1k", "13k": "9k-1k",
    "12k": "9k-1k", "11k": "9k-1k", "10k": "9k-1k",
}

GO_DATASET_SAMPLE_SIZE = {
    "1d": 1200, "2d": 1400, "3d": 1600,
    "4d": 0, "5d": 0, "6d": 0,
    "7d": 0, "8d": 0, "9d": 0,
    "Pro": 0,
}

OGS_SAMPLE_SIZE = {
    "30k-10k": 50, "9k-1k": 100, "1d-3d": 200,
    "4d-6d": 100, "7d-9d": 50, "pro": 20,
}

MIN_MOVES = 250
MAX_SCORE_DIFF = 20
GITHUB_DELAY = 0.3


# ── SGF 质量过滤 ─────────────────────────────────────
def check_sgf(path):
    """检查 SGF 文件质量，返回是否通过"""
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except:
        return False

    try:
        text = raw.decode('utf-8', errors='replace')
    except:
        return False

    # 棋盘大小
    m = re.search(r'SZ\[(\d+)\]', text)
    if m and int(m.group(1)) != 19:
        return False

    # 让子
    m = re.search(r'HA\[(\d+)\]', text)
    if m and int(m.group(1)) > 0:
        return False

    # 手数
    moves = len(re.findall(r'[BW]\[', text))
    if moves < MIN_MOVES:
        return False

    # 胜负
    m = re.search(r'RE\[([^]]+)\]', text)
    if m:
        re_str = m.group(1)
        score_m = re.match(r'[BW]\+([\d.]+)', re_str)
        if score_m:
            diff = float(score_m.group(1))
            if diff >= MAX_SCORE_DIFF:
                return False

    return True


# ── go-dataset ────────────────────────────────────────
def gh_get(url, retries=3):
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if a < retries-1:
                time.sleep(2)
                continue
            return None


def download_7z_parts(rank_dir):
    """下载某 rank 的所有 7z split parts，返回合并后的 7z 路径"""
    url = f"{API_GH}/{rank_dir}"
    items = gh_get(url)
    if not items or not isinstance(items, list):
        return []

    # 识别所有 .7z.xxx 文件和独立 .7z 文件
    parts_by_base = {}
    standalone_7z = []
    for item in items:
        name = item['name']
        m = re.match(r'^(.+?\.7z)\.(\d{3})$', name)
        if m:
            base = m.group(1)
            part_num = int(m.group(2))
            if base not in parts_by_base:
                parts_by_base[base] = []
            parts_by_base[base].append((part_num, item))
        elif name.endswith('.7z') and not re.search(r'\.7z\.\d{3}$', name):
            standalone_7z.append(item)

    result_files = []
    for base, parts in sorted(parts_by_base.items()):
        parts.sort()
        combined_path = TEMP_DIR / base

        if combined_path.exists() and combined_path.stat().st_size > 0:
            result_files.append(str(combined_path))
            continue

        print(f"    → {base} ({len(parts)} parts)...")
        with open(combined_path, 'wb') as out:
            for pn, part in parts:
                pu = f"{GO_DATASET_BASE}/{rank_dir}/{part['name']}"
                mb = part['size'] / 1024 / 1024
                print(f"      [{pn}] {part['name']} ({mb:.0f} MB)", end="", flush=True)
                for a in range(3):
                    try:
                        req = urllib.request.Request(pu, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req, timeout=300) as r:
                            out.write(r.read())
                        print(" ✓")
                        break
                    except Exception as e:
                        if a < 2:
                            print(" 重试...", end="", flush=True)
                            time.sleep(3)
                        else:
                            print(f" ✗ {e}")
                            return []
                time.sleep(GITHUB_DELAY)

    # 处理独立 .7z 文件 (无 split parts)
    for item in standalone_7z:
        name = item['name']
        combined_path = TEMP_DIR / name
        if combined_path.exists() and combined_path.stat().st_size > 0:
            result_files.append(str(combined_path))
            continue
        url = f"{GO_DATASET_BASE}/{rank_dir}/{name}"
        mb = item['size'] / 1024 / 1024
        print(f"    → {name} ({mb:.0f} MB)...", end="", flush=True)
        for a in range(3):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=300) as r:
                    with open(combined_path, 'wb') as out:
                        out.write(r.read())
                print(" ✓")
                result_files.append(str(combined_path))
                break
            except Exception as e:
                if a < 2:
                    print(" 重试...", end="", flush=True)
                    time.sleep(3)
                else:
                    print(f" ✗ {e}")

    return result_files


def extract_sample_from_7z(seven_z_path, rank_dir, n_samples):
    """从 7z 中采样提取符合条件 SGF (列表→随机选→精确提取)"""
    group = RANK_DIR_MAP[rank_dir]
    target_dir = OUTPUT_DIR / group
    target_dir.mkdir(parents=True, exist_ok=True)

    existing_ids = {f.stem for f in target_dir.glob("*.sgf")}

    # Step 1: 快速列出所有文件 (写入文件避免截断)
    print(f"      列文件清单...", end="", flush=True)
    list_file = TEMP_DIR / f"list_{rank_dir}_{random.randint(1000,9999)}.txt"
    r = subprocess.run(["7z", "l", "-ba", seven_z_path],
                       capture_output=False, stdout=open(list_file, 'w'),
                       stderr=subprocess.DEVNULL, timeout=300)
    if r.returncode != 0:
        print(f" ✗ list failed")
        return 0

    # 从文件读取
    all_names = []
    with open(list_file) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 6:
                fn = ' '.join(parts[5:])
                if fn.endswith('.sgf'):
                    all_names.append(fn)
    list_file.unlink(missing_ok=True)

    if not all_names:
        print(f" ✗ 无 SGF 文件")
        return 0

    print(f" {len(all_names)} SGF", flush=True)

    # Step 2: 随机选 3x 目标量 (留过滤余量)
    random.shuffle(all_names)
    pool = all_names[:min(n_samples * 8, len(all_names))]
    print(f"      候选 {len(pool)} 个, 提取并过滤...", flush=True)

    # Step 3: 分批提取 (每次 100 个, 避免命令行过长)
    extract_base = TEMP_DIR / f"ex_{rank_dir}_{random.randint(1000,9999)}"
    extract_base.mkdir(parents=True, exist_ok=True)

    sampled = 0
    BATCH = 20
    for i in range(0, len(pool), BATCH):
        if sampled >= n_samples:
            break
        batch = pool[i:i + BATCH]

        # 7z e 提取到同一目录 (无目录结构)
        cmd = ["7z", "e", seven_z_path, f"-o{extract_base}", "-y", "-bb0"] + batch
        r = subprocess.run(cmd, capture_output=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=600)
        if r.returncode != 0:
            print(f"      [batch {i}] 7z exit={r.returncode}, 跳过", flush=True)
            continue

        # 检查提取的文件
        for fn in batch:
            if sampled >= n_samples:
                break
            base_fn = os.path.basename(fn)
            fpath = extract_base / base_fn
            if not fpath.exists():
                continue
            base = fpath.stem
            if base in existing_ids:
                continue
            if check_sgf(fpath):
                dst = target_dir / f"{rank_dir.lower()}_{base}.sgf"
                try:
                    shutil.copy2(fpath, dst)
                    sampled += 1
                    existing_ids.add(base)
                except:
                    pass
            # 删除临时文件
            try:
                fpath.unlink()
            except:
                pass

    shutil.rmtree(extract_base, ignore_errors=True)
    print(f"      → 取 {sampled}/{n_samples} (从 {len(pool)} 候选)", flush=True)
    return sampled


def download_go_dataset():
    print("\n[go-dataset] 开始下载高段位...")

    existing = {}
    for g in RANK_TARGETS:
        d = OUTPUT_DIR / g
        existing[g] = len(list(d.glob("*.sgf"))) if d.exists() else 0

    needed = []
    for rank_dir, group in RANK_DIR_MAP.items():
        target = RANK_TARGETS[group]
        have = existing[group]
        need = max(0, target - have)
        take = min(GO_DATASET_SAMPLE_SIZE.get(rank_dir, 0), need)
        if take > 0:
            needed.append((rank_dir, group, take))

    if not needed:
        print("  所有段位已达标，跳过")
        return 0

    print(f"  需补充 {len(needed)} 个段位:")
    for r, g, s in needed:
        print(f"    {r:>4s} → {g:>10s}: 取 {s:>4d}")

    total = 0
    for rank_dir, group, sample_size in needed:
        print(f"\n  [{rank_dir}]...", end="", flush=True)
        seven_z_files = download_7z_parts(rank_dir)
        if not seven_z_files:
            print(" ✗ 下载失败")
            continue

        for szf in seven_z_files:
            taken = extract_sample_from_7z(szf, rank_dir, sample_size - total)
            total += taken
            if total >= sample_size:
                break

        # 清理 7z
        for szf in seven_z_files:
            try:
                os.remove(szf)
            except:
                pass

    print(f"\n  go-dataset 合计: {total} 盘")
    return total


# ── OGS ───────────────────────────────────────────────
OGS_COOKIE = "/tmp/ogs_cookies.txt"
OGS_API = "https://online-go.com/api/v1"


def ogs_get(path, params=None):
    url = f"{OGS_API}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    cmd = ["curl", "-s", "-b", OGS_COOKIE, "-w", "\n%{http_code}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = r.stdout.strip().rsplit("\n", 1)
        if len(lines) < 2 or lines[-1] != "200":
            return None
        return json.loads(lines[0]) if lines[0] else None
    except:
        return None


def rank_to_group(r):
    if r is None:
        return None
    if r >= 39:
        return "pro"
    elif r >= 30:
        d = r - 29
        return "1d-3d" if d <= 3 else "4d-6d" if d <= 6 else "7d-9d"
    else:
        return "30k-10k" if (30 - r) >= 10 else "9k-1k"


def download_ogs():
    print("\n[OGS] BFS 少量采样...")
    test = ogs_get("/me/games", {"page_size": 1})
    if not test:
        print("  ⚠ 认证失败，跳过")
        return 0

    existing = {}
    for g in RANK_TARGETS:
        d = OUTPUT_DIR / g
        existing[g] = len(list(d.glob("*.sgf"))) if d.exists() else 0

    targets = {}
    for g, t in RANK_TARGETS.items():
        need = max(0, t - existing[g])
        take = min(OGS_SAMPLE_SIZE.get(g, 0), need)
        if take > 0:
            targets[g] = take

    if not targets:
        return 0

    total_needed = sum(targets.values())
    collected = {g: 0 for g in targets}
    downloaded_ids = set()
    queue = set([1052589])
    visited = set()
    total = 0
    attempts = 0

    print(f"  目标: {total_needed} 盘")

    while total < total_needed and queue and attempts < 500:
        attempts += 1
        pid = queue.pop()
        if pid in visited:
            continue
        visited.add(pid)

        data = ogs_get(f"/players/{pid}/games", {"page_size": 50})
        if not data or not data.get('results'):
            continue

        for g in data['results']:
            if total >= total_needed:
                break
            gid = g.get('id')
            if not gid or gid in downloaded_ids:
                continue
            if not g.get('ranked') or g.get('handicap', 0) > 0:
                continue
            if g.get('width') != 19 or g.get('height') != 19:
                continue

            players = g.get('players', {})
            if any('bot' in str(p.get('ui_class', '')).lower() for p in players.values()):
                continue

            detail = ogs_get(f"/games/{gid}")
            time.sleep(0.3)
            if not detail:
                continue
            gd = detail.get('gamedata', {})
            if not gd:
                continue

            moves = len(gd.get('moves', []))
            if moves < MIN_MOVES:
                continue

            pl = gd.get('players', {})
            ranks = [p.get('rank') for p in pl.values() if p.get('rank') is not None]
            if not ranks:
                continue

            group = rank_to_group(max(ranks))
            if not group or group not in targets or collected[group] >= targets[group]:
                continue

            fpath = OUTPUT_DIR / group / f"ogs_{gid}.sgf"
            if fpath.exists():
                continue

            cmd = ["curl", "-s", "-b", OGS_COOKIE, "-o", str(fpath),
                   "-w", "%{http_code}", f"{OGS_API}/games/{gid}/sgf"]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.stdout.strip() == "200":
                    downloaded_ids.add(gid)
                    collected[group] += 1
                    total += 1
                    for c in ('black', 'white'):
                        opp = detail.get(c)
                        if opp and opp not in visited and opp != pid:
                            queue.add(opp)
            except:
                pass
            time.sleep(0.3)

    print(f"  OGS 采样: {total} 盘")
    return total


# ── 统计 ──────────────────────────────────────────────
def show_stats():
    final = {}
    for g in RANK_TARGETS:
        d = OUTPUT_DIR / g
        final[g] = len(list(d.glob("*.sgf"))) if d.exists() else 0

    print(f"\n{'分组':>10s}  {'目标':>6s}  {'已有':>6s}  {'完成率':>6s}")
    print("-" * 35)
    for g, t in RANK_TARGETS.items():
        c = final[g]
        pct = c / t * 100 if t else 0
        mark = " ✓" if c >= t else ""
        print(f"{g:>10s}  {t:>5d}  {c:>5d}  {pct:>5.0f}%{mark}")

    total = sum(final.values())
    total_t = sum(RANK_TARGETS.values())
    print("-" * 35)
    print(f"{'总计':>10s}  {total_t:>5d}  {total:>5d}  "
          f"{total/total_t*100:>5.0f}%")
    return final


# ── 主入口 ────────────────────────────────────────────
def main():
    print("=" * 60)
    print(" 三源合一下载 — 正态分布训练数据集")
    print("=" * 60)

    print("当前状态:")
    show_stats()

    # Step 1: go-dataset
    print(f"\n{'='*60}")
    print(" Step 1: go-dataset (featurecat/go-dataset)")
    print("  来源: 21.1M 围棋棋谱, 18k-9p")
    print("  过滤: >250手 | 无让子 | 胜负<10子")
    print(f"{'='*60}")
    gd = download_go_dataset()

    # Step 2: OGS 少量
    print(f"\n{'='*60}")
    print(" Step 2: OGS BFS (少量掺杂)")
    print(f"{'='*60}")
    ogs = download_ogs()

    # 最终
    print(f"\n{'='*60}")
    print(" 完成")
    print(f"{'='*60}")
    print(f"  go-dataset: {gd} 盘")
    print(f"  OGS:        {ogs} 盘")
    print(f"  GTL:        (已有)")
    f = show_stats()

    # 清理临时
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
        print("\n临时文件已清理")


if __name__ == '__main__':
    main()

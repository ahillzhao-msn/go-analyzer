#!/usr/bin/env python3
"""OGS Pro 棋谱下载 — 从职业棋手 BFS"""
import json, os, subprocess, time, random
from pathlib import Path

OUT = Path("/mnt/c/users/ahill/documents/python/go_analysis_project/training/pro")
OUT.mkdir(parents=True, exist_ok=True)
COOKIE = "/tmp/ogs_cookies.txt"
API = "https://online-go.com/api/v1"
MIN_MOVES = 250
MAX_SCORE = 20
TARGET = 800

def curl(path, params=None):
    url = f"{API}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k,v in params.items())
        url = f"{url}?{qs}"
    cmd = ["curl", "-s", "-b", COOKIE, "-w", "\n%{http_code}", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    lines = r.stdout.strip().rsplit("\n", 1)
    if len(lines) < 2 or lines[-1] != "200": return None
    return json.loads(lines[0]) if lines[0] else None

def download(game_id, path):
    if path.exists(): return True
    cmd = ["curl", "-s", "-b", COOKIE, "-o", str(path), "-w", "%{http_code}",
           f"{API}/games/{game_id}/sgf"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return r.stdout.strip() == "200"

existing = len(list(OUT.glob("*.sgf")))
print(f"已有 Pro: {existing}/{TARGET}")
if existing >= TARGET:
    print("已达标")
    exit(0)

# Get pro players
pros = curl("/players", {"professional": "true", "page_size": 73})
if not pros: print("OGS API 失败"); exit(1)
pro_ids = [p['id'] for p in pros.get('results', [])]
print(f"职业棋手: {len(pro_ids)} 人")

queue = set(pro_ids[:20])  # 每页 20 人
visited = set()
downloaded_ids = set()
total = existing

for f in OUT.glob("*.sgf"):
    m = f.stem.split("_")
    if len(m) > 1 and m[-1].isdigit():
        downloaded_ids.add(int(m[-1]))

while queue and total < TARGET:
    pid = queue.pop()
    if pid in visited: continue
    visited.add(pid)

    games = curl(f"/players/{pid}/games", {"page_size": 50})
    if not games or not games.get('results'): continue

    for g in games['results']:
        if total >= TARGET: break
        gid = g.get('id')
        if not gid or gid in downloaded_ids: continue
        if not g.get('ranked') or g.get('handicap', 0) > 0: continue
        if g.get('width') != 19 or g.get('height') != 19: continue

        detail = curl(f"/games/{gid}")
        time.sleep(0.3)
        if not detail: continue

        gd = detail.get('gamedata', {})
        if not gd: continue
        if len(gd.get('moves', [])) < MIN_MOVES: continue

        fpath = OUT / f"ogs_pro_{gid}.sgf"
        if fpath.exists(): continue

        if download(gid, fpath):
            downloaded_ids.add(gid)
            total += 1
            print(f"  [{total}/{TARGET}] +{gid}", flush=True)
            for c in ('black', 'white'):
                opp = detail.get(c)
                if opp and opp not in visited and opp != pid:
                    queue.add(opp)
        time.sleep(0.3)

    if len(queue) < 5 and len(pro_ids) > 0:
        # 补充职业棋手
        more = random.sample([p for p in pro_ids if p not in visited], 
                           min(10, len([p for p in pro_ids if p not in visited])))
        queue.update(more)

print(f"\n完成! Pro: {total}/{TARGET}")

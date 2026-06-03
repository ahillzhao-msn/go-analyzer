#!/usr/bin/env python3
"""Go Analyzer Worker — 在远程主机本地运行 KataGo 分析.

用法:
  python worker.py <sgf_dir> <output_dir> [--visits 25]

不需要 numpy/torch, 只需 Python 标准库 + kata-go.
输出: <output_dir>/<game_id>.json
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def parse_moves(sgf_content: str) -> list:
    """简易 SGF 解析: 只取主线, 输出 [player, gtp]"""
    moves = []
    i = sgf_content.find(";")
    while i >= 0:
        j = sgf_content.find(";", i + 1)
        node_str = sgf_content[i + 1:j] if j > 0 else sgf_content[i + 1:]
        # Find B[..] or W[..] moves
        for player in ("B", "W"):
            idx = node_str.find(f"{player}[")
            if idx >= 0:
                end = node_str.find("]", idx)
                if end > 0:
                    coord = node_str[idx + 2:end]
                    if coord and coord.lower() not in ("tt", "pass", ""):
                        moves.append([player, coord.upper()])
        i = j
    return moves


def build_query(game_id: str, moves: list, visits: int) -> str:
    """构建 KataGo 查询."""
    return json.dumps({
        "id": game_id,
        "moves": moves,
        "maxVisits": visits,
        "rules": "chinese",
        "komi": 7.5,
        "boardXSize": 19,
        "boardYSize": 19,
        "includePolicy": True,
    })


def analyze_game(katago: str, model: str, config: str,
                 sgf_content: str, game_id: str, visits: int) -> dict:
    """运行 KataGo 分析一局棋, 返回所有位置的结果."""
    all_moves = parse_moves(sgf_content)
    if not all_moves:
        return {"game_id": game_id, "success": False, "error": "No moves"}

    cmd = [katago, "analysis", "-model", model, "-config", config]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    time.sleep(1)
    if proc.poll() is not None:
        return {"game_id": game_id, "success": False, "error": "KataGo failed to start"}

    # Send all position queries
    queries = []
    for idx in range(len(all_moves)):
        history = all_moves[:idx]
        queries.append(build_query(f"{game_id}_{idx}", history, visits))

    proc.stdin.write("\n".join(queries) + "\n")
    proc.stdin.flush()
    proc.stdin.close()

    # Read all responses
    responses = {}
    for line in proc.stdout:
        if not line:
            break
        resp = json.loads(line.strip())
        rid = resp.get("id", "")
        parts = rid.split("_")
        if parts:
            try:
                responses[int(parts[-1])] = resp
            except ValueError:
                pass

    proc.wait(timeout=120)

    return {
        "game_id": game_id,
        "success": len(responses) > 0,
        "moves_analyzed": len(responses),
        "total_moves": len(all_moves),
        "responses": responses,
    }


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <sgf_dir> <output_dir> [--visits N]", file=sys.stderr)
        sys.exit(1)

    sgf_dir = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])
    visits = 25
    if "--visits" in sys.argv:
        idx = sys.argv.index("--visits")
        visits = int(sys.argv[idx + 1])

    katago = os.environ.get("KATAGO_PATH",
                            "C:/Users/xiaoj/go-analyzer-worker/katago/katago.exe")
    model = os.environ.get("KATAGO_MODEL",
                           "C:/Users/xiaoj/go-analyzer-worker/models/kata1-b18c384nbt-s6582191360-d3422816034.bin.gz")
    config = os.environ.get("KATAGO_CONFIG",
                            "C:/Users/xiaoj/go-analyzer-worker/config/analysis_config.cfg")

    output_dir.mkdir(parents=True, exist_ok=True)

    sgf_files = sorted(sgf_dir.glob("*.sgf"))
    print(f"Worker: {len(sgf_files)} SGF files, {visits} visits", flush=True)

    ok = fail = 0
    t_start = time.time()

    for sgf_path in sgf_files:
        content = sgf_path.read_text(encoding="utf-8", errors="replace")
        t0 = time.time()
        result = analyze_game(katago, model, config, content,
                               sgf_path.stem, visits)

        if result["success"]:
            out_path = output_dir / f"{sgf_path.stem}.json"
            with open(out_path, "w") as f:
                json.dump(result["responses"], f)
            ok += 1
        else:
            fail += 1

        dt = time.time() - t0
        rate = (ok + fail) / (time.time() - t_start) * 60
        print(f"[{time.strftime('%H:%M:%S')}] {sgf_path.stem}: "
              f"{'OK' if result['success'] else 'FAIL'} "
              f"({dt:.1f}s) [{ok}/{fail}] {rate:.1f}/min", flush=True)

    elapsed = time.time() - t_start
    print(f"\nWorker done: {ok} OK, {fail} FAIL in {elapsed:.0f}s", flush=True)


if __name__ == "__main__":
    main()

"""全量 260 局分析 — 标准模型 25 visits, 适配器直连。"""
import sys, os
sys.path.insert(0, 'src')
from go_analysis.analyzer import create_adapter
from go_analysis.analysis_format import AnalysisRecord, AnalysisStore
from go_analysis.env_collector import collect_hardware, collect_software, extract_game_meta_from_sgf_file
from go_analysis.models import extract_features_from_analysis, compute_global_stats
import numpy as np

SGF_DIR = "/mnt/c/Users/ahill/Documents/Python/Bob"
OUTPUT = "analysis_store"
VISITS = 25
BATCH = 25  # 每次处理 25 局，分批渐进

store = AnalysisStore(OUTPUT)
existing = store.list_games()
print(f"Existing: {len(existing)} games")

sgf_files = sorted([f for f in os.listdir(SGF_DIR) if f.endswith('.sgf')])
pending = []
for sgf in sgf_files:
    gid = sgf.replace('.sgf', '')
    if not os.path.exists(os.path.join(OUTPUT, f"{gid}.npz")):
        pending.append(sgf)
print(f"Pending: {len(pending)} games")
pending = pending[:BATCH]

if not pending:
    print("All done!")
    sys.exit(0)

hw = collect_hardware()
sw = collect_software(max_visits=VISITS)
adapter = create_adapter(platform="windows_native", visits=VISITS)

ok, fail = 0, 0
for sgf_name in pending:
    sgf_path = os.path.join(SGF_DIR, sgf_name)
    game_id = sgf_name.replace('.sgf', '')
    game = extract_game_meta_from_sgf_file(sgf_path, game_id=game_id)
    if game.total_moves < 10:
        print(f"  SKIP {game_id}: {game.total_moves} moves"); continue
    
    with open(sgf_path) as f: content = f.read()
    result = adapter.analyze(content, game_id=game_id, visits=VISITS)
    if not result.success:
        print(f"  FAIL {game_id}: {str(result.error)[:60]}")
        fail += 1; continue
    
    fl = result.raw_json["features_list"]
    feats = np.stack([f["features"] for f in fl], axis=0)
    gs = compute_global_stats([type("o",(),{"features":f["features"]})() for f in fl])
    rec = AnalysisRecord.compress(feats, gs, hw, sw, game)
    store.put(game_id, rec)
    print(f"  [{ok+1}/{len(pending)}] {game_id}: {len(fl)} pos, {result.duration_s:.0f}s")
    ok += 1

adapter.shutdown()
print(f"\nBatch: {ok} done, {fail} failed, {len(pending)-ok-fail} skipped")
print(f"Total store: {len(store.list_games())} games")

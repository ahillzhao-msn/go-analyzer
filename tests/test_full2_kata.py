"""Correct per-position analysis: analyze pos P-1, find move at P"""
import json, subprocess, time, os, sys
sys.path.insert(0, 'src')
from go_analysis.sgf_parser import SGF

PROJ = os.environ.get('GO_ANALYSIS_PROJ', '.')
WIN_PROJ = os.environ.get('GO_ANALYSIS_WIN_PROJ', '.')
KATAGO = os.path.join(PROJ, 'kata-go', 'windows', 'v1.16.4', 'katago.exe')
MODEL = os.path.join(WIN_PROJ, 'kata-go', 'models', 'kata1-b18c384nbt-s6582191360-d3422816034.bin.gz')
CFG = os.path.join(WIN_PROJ, 'kata-go', 'windows', 'analysis_config.cfg')

# Parse SGF
sgf_path = os.path.join(PROJ, '..', 'Bob', 'yunyi_100_101_237851.sgf')
tree = SGF.parse_file(sgf_path)
all_moves = []
for n in tree.nodes_in_tree:
    m = n.move
    if m and not m.is_pass:
        all_moves.append([m.player, m.gtp()])
print(f'Total moves: {len(all_moves)}')

# Start KataGo
proc = subprocess.Popen([KATAGO,'analysis','-model',MODEL,'-config',CFG],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1)
time.sleep(5)
print(f'Running, alive={proc.poll() is None}')

if proc.poll() is not None:
    print(f'CRASHED: {proc.stderr.read()[:500]}')
    sys.exit(1)

# Analyze positions 1, 51, 101, 151 (analyze AFTER 0, 50, 100, 150 moves)
# and find the NEXT move in the candidate list
for query_idx in [0, 50, 100, 150, 200]:
    if query_idx >= len(all_moves): break
    history = all_moves[:query_idx]  # moves played so far
    next_move = all_moves[query_idx]  # the move we want to evaluate
    
    q = json.dumps({
        'id': f'q{query_idx}',
        'moves': history,
        'maxVisits': 50,
        'rules': 'chinese', 'komi': 7.5,
        'boardXSize': 19, 'boardYSize': 19,
        'includePolicy': True,
    })
    t0 = time.time()
    proc.stdin.write(q + '\n')
    proc.stdin.flush()
    
    line = ''
    for _ in range(300):
        r = proc.stdout.readline()
        if r: line = r.strip(); break
        time.sleep(0.1)
    
    if not line:
        print(f'Q{query_idx}: TIMEOUT ({time.time()-t0:.0f}s)')
        break
    
    result = json.loads(line)
    mi = result.get('moveInfos', [])
    
    # Find the next move in the candidate list
    player, move_coord = next_move
    found = None
    for m in mi:
        if m['move'] == move_coord:
            found = m
            break
    
    if found:
        print(f'Q{query_idx:+3d} ({time.time()-t0:.1f}s): {player}{move_coord} → '
              f'order={found["order"]:2d} prior={found["prior"]:.4f} '
              f'wr={found["winrate"]:.3f} sl={found["scoreLead"]:.1f} '
              f'lcb={found.get("lcb","N/A")}')
    else:
        print(f'Q{query_idx:+3d} ({time.time()-t0:.1f}s): {player}{move_coord} → '
              f'NOT IN TOP ({len(mi)} cands: {[m["move"] for m in mi[:5]]}...)')

proc.terminate()
print('\nDONE')

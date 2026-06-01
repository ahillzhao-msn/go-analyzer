"""Full pipeline test with KataGo v1.13.0 (working)"""
import json, subprocess, time, sys, os
sys.path.insert(0, 'src')
from go_analysis.sgf_parser import SGF

KATAGO = os.environ.get('KATAGO', 'katago')
MODEL = os.environ.get('KATAGO_MODEL', '')
CFG = os.environ.get('KATAGO_CONFIG', '')

# Parse SGF
sgf_path = os.path.join(os.environ.get('SGF_DIR', '/path/to/sgfs'), 'yunyi_100_101_237851.sgf')
tree = SGF.parse_file(sgf_path)
moves = []
for n in tree.nodes_in_tree:
    m = n.move
    if m and not m.is_pass:
        moves.append([m.player, m.gtp()])
print(f'Moves: {len(moves)}')

# Start persistent KataGo
proc = subprocess.Popen([KATAGO,'analysis','-model',MODEL,'-config',CFG],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    text=True, bufsize=1)
time.sleep(4)
print(f'KataGo ready (PID={proc.pid})')

# Analyze positions 0, 50, 100, 150, 200, 250
positions = [0, 50, 100, 150, 200, 250]
features_all = []

for pos in positions:
    if pos >= len(moves): continue
    sub_moves = moves[:pos+1]
    query = json.dumps({
        'id': f'p{pos}', 'moves': sub_moves,
        'maxVisits': 50,
        'rules': 'chinese', 'komi': 7.5,
        'boardXSize': 19, 'boardYSize': 19,
        'includePolicy': True,
    })
    t0 = time.time()
    proc.stdin.write(query + '\n')
    proc.stdin.flush()
    line = ''
    for _ in range(200):  # up to 20s wait
        r = proc.stdout.readline()
        if r:
            line = r.strip()
            break
        time.sleep(0.1)
    elapsed = time.time() - t0
    
    if not line:
        print(f'  Pos {pos}: TIMEOUT ({elapsed:.0f}s)')
        break
    
    result = json.loads(line)
    mi = result.get('moveInfos', [])
    player_move = moves[pos][1]
    
    # Find player's move info
    player_info = None
    for m in mi:
        if m['move'] == player_move:
            player_info = m
            break
    
    if player_info:
        print(f'  Pos {pos:3d} ({elapsed:.1f}s): {player_move} order={player_info["order"]:2d} prior={player_info["prior"]:.4f} wr={player_info["winrate"]:.3f}')
        features_all.append(player_info)
    else:
        print(f'  Pos {pos:3d} ({elapsed:.1f}s): {player_move} NOT IN TOP ({len(mi)} candidates)')

if features_all:
    print(f'\n✅ Got {len(features_all)} feature sets from {len(positions)} positions')
    print(f'Sample: order={features_all[0]["order"]} prior={features_all[0]["prior"]:.4f} '
          f'winrate={features_all[0]["winrate"]:.3f} scoreLead={features_all[0]["scoreLead"]:.1f}')
else:
    print(f'\n❌ No features extracted')

proc.terminate()
print('Done')

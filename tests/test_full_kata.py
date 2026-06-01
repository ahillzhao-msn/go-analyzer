"""Full pipeline test — persistent KataGo v1.16.4 + SGF moves"""
import json, subprocess, time, os, sys
sys.path.insert(0, 'src')
from go_analysis.sgf_parser import SGF

PROJ = os.environ.get('GO_ANALYSIS_PROJ', '.')
WIN_PROJ = os.environ.get('GO_ANALYSIS_WIN_PROJ', '.')
KATAGO = os.path.join(PROJ, 'kata-go', 'windows', 'v1.16.4', 'katago.exe')
MODEL = os.path.join(WIN_PROJ, 'kata-go', 'models', 'kata1-b18c384nbt-s6582191360-d3422816034.bin.gz')
CFG = os.path.join(WIN_PROJ, 'kata-go', 'windows', 'analysis_config.cfg')

# Fix tuning cache
os.system(f'cp -r {PROJ}/kata-go/KataGoData {PROJ}/kata-go/ 2>/dev/null')

sgf_path = os.path.join(PROJ, '..', 'Bob', 'yunyi_100_101_237851.sgf')
tree = SGF.parse_file(sgf_path)
moves = []
for n in tree.nodes_in_tree:
    m = n.move
    if m and not m.is_pass:
        moves.append([m.player, m.gtp()])
print(f'Moves: {len(moves)}')

proc = subprocess.Popen([KATAGO,'analysis','-model',MODEL,'-config',CFG],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1)
time.sleep(3)
print(f'KataGo PID={proc.pid} ready={proc.poll() is None}')

if proc.poll() is not None:
    print(f'CRASHED: {proc.stderr.read()[:500]}')
    sys.exit(1)

# Test 6 positions
for pos in [0, 50, 100, 150, 200, 250]:
    if pos >= len(moves): continue
    q = json.dumps({'id':f'p{pos}','moves':moves[:pos+1],
        'maxVisits':50,'rules':'chinese','komi':7.5,
        'boardXSize':19,'boardYSize':19,'includePolicy':True})
    t0 = time.time()
    proc.stdin.write(q+chr(10)); proc.stdin.flush()
    line = ''
    for _ in range(300):
        r = proc.stdout.readline()
        if r: line=r.strip(); break
        time.sleep(0.1)
    if not line:
        print(f'P{pos:3d}: TIMEOUT ({time.time()-t0:.0f}s)'); break
    result = json.loads(line)
    mi = result.get('moveInfos',[])
    pm = moves[pos][1]
    for m in mi:
        if m['move'] == pm:
            print(f'P{pos:3d} ({time.time()-t0:.1f}s): {pm} order={m["order"]:2d} prior={m["prior"]:.4f} wr={m["winrate"]:.3f}')
            break
    else:
        print(f'P{pos:3d}: {pm} NOT IN TOP ({len(mi)} cands)')

proc.terminate()
print('DONE')

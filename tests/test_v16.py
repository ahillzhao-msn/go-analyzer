"""Test KataGo v1.16.5 persistent process."""
import json, subprocess, time, os, sys

PROJ = os.environ.get('GO_ANALYSIS_PROJ', '.')
WIN_PROJ = os.environ.get('GO_ANALYSIS_WIN_PROJ', '.')
KATAGO = os.path.join(PROJ, 'kata-go', 'windows', 'katago-v1.16.5-opencl-windows-x64.exe')
MODEL = os.path.join(WIN_PROJ, 'kata-go', 'models', 'kata1-b18c384nbt-s6582191360-d3422816034.bin.gz')
CFG = os.path.join(WIN_PROJ, 'kata-go', 'windows', 'analysis_config.cfg')

# Verify files exist
for p, name in [
    (KATAGO, "katago.exe"),
    (os.path.join(PROJ, 'kata-go', 'models', 'kata1-b18c384nbt-s6582191360-d3422816034.bin.gz'), 'model'),
    (os.path.join(PROJ, 'kata-go', 'windows', 'analysis_config.cfg'), 'config'),
]:
    exists = os.path.exists(p)
    print(f'  {exists} {name}: {p}')
    if not exists:
        print(f'  MISSING!')
        sys.exit(1)

# Start KataGo
print(f'\nStarting KataGo...')
proc = subprocess.Popen(
    [KATAGO, 'analysis', '-model', MODEL, '-config', CFG],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1
)
pid = proc.pid
print(f'PID: {pid}')

# Wait for initialization
time.sleep(5)
alive = proc.poll() is None
print(f'After 5s: alive={alive}')

if not alive:
    err = proc.stderr.read()[:1000]
    print(f'CRASHED (code {proc.returncode}): {err}')
    sys.exit(1)

# Send query
q = json.dumps({
    'id': 't1',
    'moves': [['B', 'pd'], ['W', 'dd'], ['B', 'dp']],
    'maxVisits': 50,
    'rules': 'chinese',
    'komi': 7.5,
    'boardXSize': 19,
    'boardYSize': 19,
    'includePolicy': True,
})
print(f'Sending query ({len(q)} bytes)...')
proc.stdin.write(q + '\n')
proc.stdin.flush()

# Read response
start = time.time()
line = ''
while time.time() - start < 30:
    r = proc.stdout.readline()
    if r:
        line = r.strip()
        break
    time.sleep(0.2)

elapsed = time.time() - start

if line:
    result = json.loads(line)
    mi = result.get('moveInfos', [])
    print(f'\nRESPONSE ({elapsed:.1f}s): {len(mi)} moves analyzed')
    if 'error' in result:
        print(f'  ERROR: {result["error"]}')
    if mi:
        for m in mi[:3]:
            print(f'  {m["move"]}: order={m["order"]} prior={m["prior"]:.4f} wr={m["winrate"]:.3f}')
else:
    print(f'\nTIMEOUT ({elapsed:.0f}s) - no response')
    err = proc.stderr.read()[:500]
    if err:
        print(f'Stderr: {err}')

proc.terminate()
proc.wait()
print(f'Done (exit {proc.returncode})')

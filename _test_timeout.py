"""Diagnose WindowsProcess.readline timeout behavior.
Starts KataGo, sends 5 query lines, reads responses with timeout.
"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

from go_analysis.analyzer.process import WindowsProcess
from go_analysis.analyzer.base import moves_to_katago_format

KATAGO = r"C:\Users\xiaoj\go-analyzer-worker\katago\katago.exe"
MODEL = r"C:\Users\xiaoj\go-analyzer-worker\models\kata1-b18c384nbt-s6582191360-d3422816034.bin.gz"
CONFIG = r"C:\Users\xiaoj\go-analyzer-worker\analysis_config.cfg"

print("[1] Starting KataGo...", flush=True)
p = WindowsProcess()
p.start(KATAGO, MODEL, CONFIG)
print("[2] Started. Sending 3 test queries...", flush=True)

test_moves = [
    [{"x":3,"y":3},{"x":15,"y":15},{"x":3,"y":15},{"x":15,"y":3},{"x":9,"y":9}],
    [{"x":3,"y":3},{"x":15,"y":15},{"x":3,"y":15},{"x":15,"y":3},{"x":9,"y":9},{"x":9,"y":3},{"x":3,"y":9},{"x":15,"y":9}],
    [{"x":3,"y":3},{"x":15,"y":15}],
]

for i, moves in enumerate(test_moves):
    query = f'{{"id":"t{i}","moves":{moves_to_katago_format(moves)},"maxVisits":25,"rules":"chinese","komi":7.5,"boardXSize":19,"boardYSize":19,"includePolicy":false}}\n'
    try:
        p.send(query)
        print(f"  [{i}] Sent {len(moves)} moves", flush=True)
    except Exception as e:
        print(f"  [{i}] SEND ERROR: {e}", flush=True)
        p.kill()
        sys.exit(1)

print("[3] Reading responses with 10s timeout each...", flush=True)
for i in range(3):
    deadline = time.time() + 10.0
    t0 = time.time()
    line = p.readline(deadline)
    dt = time.time() - t0
    if line is None:
        print(f"  [{i}] TIMEOUT after {dt:.1f}s (None returned)", flush=True)
    elif line == "":
        print(f"  [{i}] EOF after {dt:.1f}s", flush=True)
    else:
        print(f"  [{i}] OK ({dt:.1f}s): {line[:80]}...", flush=True)

print("[4] Cleaning up...", flush=True)
p.kill()
print("DONE", flush=True)

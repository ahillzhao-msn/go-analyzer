"""Simulate production worker: process 5 games sequentially via WindowsAnalyzer."""
import sys, os, json, time, glob

sys.path.insert(0, r"C:\Users\xiaoj\go-analyzer-worker\venv\lib\site-packages")
os.environ["PYTHONIOENCODING"] = "utf-8"

from go_analysis.analyzer import WindowsAnalyzer
from go_analysis.analysis.sgf_parser import extract_main_line

a = WindowsAnalyzer(
    katago_path=r"C:\Users\xiaoj\go-analyzer-worker\katago\katago.exe",
    model_path=r"C:\Users\xiaoj\go-analyzer-worker\models\kata1-b18c384nbt-s6582191360-d3422816034.bin.gz",
    config_path=r"C:\Users\xiaoj\go-analyzer-worker\analysis_config.cfg",
    visits=25, batch_timeout=60.0,
    max_games=10, max_age=300,
)

sgf_dir = r"C:\Users\xiaoj\go-analyzer-worker\training"
sgfs = sorted(glob.glob(os.path.join(sgf_dir, "**", "*.sgf"), recursive=True))[:10]
print(f"Searching {sgf_dir}... found {len(glob.glob(os.path.join(sgf_dir, '**', '*.sgf'), recursive=True))} total")

print(f"Testing {len(sgfs)} games sequentially...")
t_start = time.time()
for i, sgf_path in enumerate(sgfs):
    with open(sgf_path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    moves = extract_main_line(content)
    if len(moves) < 10:
        print(f"  [{i+1}] {os.path.basename(sgf_path)}: {len(moves)}m too short, skip")
        continue
    t0 = time.time()
    result = a.analyze(moves[:50])
    dt = time.time() - t0
    if result.success:
        vps = len(moves[:50]) * 25 / max(dt, 0.1)
        print(f"  [{i+1}] {os.path.basename(sgf_path)}: OK {len(moves)}m {dt:.1f}s {vps:.0f}vps")
    else:
        print(f"  [{i+1}] {os.path.basename(sgf_path)}: FAIL {len(moves)}m {dt:.1f}s")
        print("  *** Would test readline timeout behavior ***")

elapsed = time.time() - t_start
print(f"\nAll done in {elapsed:.0f}s")
a.shutdown()

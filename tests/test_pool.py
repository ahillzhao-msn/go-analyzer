"""Test AnalysisPool with 2 workers, 3 games."""
import sys, os, time
sys.path.insert(0, 'src')

from go_analysis.analyzer import AnalysisPool, AnalysisTask, TaskPriority
from go_analysis.analyzer.pool import PoolConfig
from go_analysis.analysis_format import AnalysisStore
from go_analysis.analyzer.adapters.windows_native import WindowsNativeAdapter

SGF_DIR = os.environ.get('SGF_DIR', '/path/to/sgf/files')
OUTPUT = os.environ.get('TEST_OUTPUT', 'test_pool_output')

# Pool with 2 workers
config = PoolConfig(
    max_workers=2,
    default_visits=50,
    store_dir=OUTPUT,
)

pool = AnalysisPool(
    config=config,
    adapter_factory=lambda: WindowsNativeAdapter(visits=50),
)

# Start
pool.start()
print(f'Pool started: {pool.status()}')

# Submit 3 games
import glob
sgf_files = sorted(glob.glob(os.path.join(SGF_DIR, "*.sgf")))[:3]
print(f'Submitting {len(sgf_files)} games...')

for path in sgf_files:
    game_id = os.path.splitext(os.path.basename(path))[0]
    task = AnalysisTask(sgf_path=path, game_id=game_id, visits=50)
    pool.submit(task)

# Monitor progress
try:
    for i in range(120):  # up to 120 iterations (2 min)
        time.sleep(5)
        s = pool.status()
        print(f'  [{i*5:3d}s] queued={s["queued"]} running={s["running"]} '
              f'completed={s["completed"]} failed={s["failed"]}')
        if s['completed'] + s['failed'] >= len(sgf_files):
            break
except KeyboardInterrupt:
    print('Interrupted')

# Results
print(f'\nFinal status: {pool.status()}')
pool.shutdown(wait=True)

# Show store
store = AnalysisStore(OUTPUT)
games = store.list_games()
print(f'\nStored: {len(games)} games')
for gid in games:
    r = store.get(gid)
    if r:
        print(f'  {gid}: moves={r.move_count} features={r.features.shape} '
              f'game={r.game.player_black[:6]} vs {r.game.player_white[:6]}')
st = store.stats()
print(f'\nTotal: {st["game_count"]} games, {st["total_mb"]:.1f}MB')
print('DONE')

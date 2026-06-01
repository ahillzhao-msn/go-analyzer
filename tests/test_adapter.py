"""Test WindowsNativeAdapter with a real SGF file."""
import sys, os, json
sys.path.insert(0, 'src')

from go_analysis.analyzer.adapters.windows_native import WindowsNativeAdapter
import numpy as np

SGF_DIR = os.environ.get('SGF_DIR', '/path/to/sgf/files')
SGF_FILE = os.environ.get('SGF_FILE', 'example.sgf')
sgf_path = os.path.join(SGF_DIR, SGF_FILE)
with open(sgf_path) as f:
    sgf = f.read()

# Create adapter
adapter = WindowsNativeAdapter(visits=50)
adapter.start()
print(f'Adapter info: {json.dumps(adapter.info(), indent=2)}')

# Analyze
print(f'\nAnalyzing game ({len(sgf)} bytes, ~300 moves at 50 visits)...')
result = adapter.analyze(sgf, game_id='test001', visits=50)
print(f'\nResult: success={result.success}')
print(f'  duration={result.duration_s:.0f}s')
print(f'  moves={result.move_count}')
print(f'  visits_used={result.visits_used}')

if result.success:
    print(f'  features_shape={result.raw_json["features_shape"]}')
    print(f'  positions_analyzed={result.raw_json["positions_analyzed"]}')
    rate = result.raw_json["positions_analyzed"] / result.duration_s
    print(f'  rate={rate:.1f} positions/s')

adapter.shutdown()
print('\nDONE')

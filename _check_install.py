"""Check which wheel is actually installed in worker venv"""
import importlib.metadata

for dist in importlib.metadata.distributions():
    if dist.metadata["Name"] == "go-analyzer":
        print(f"Installed: {dist.metadata['Name']} {dist.metadata['Version']}")
        print(f"Location: {dist._path}")
        break
else:
    print("go-analyzer not found in installed packages")

# Also check that the key fix is present
from go_analysis.analyzer.base import moves_to_katago_format, _GTP_COLUMNS
print(f"moves_to_katago_format: {moves_to_katago_format}")
test = moves_to_katago_format([{"x": 3, "y": 3}])
print(f"Test conversion: {test}")
assert test == [["B", "D4"]], f"Wrong format: {test}"
print("moves_to_katago_format: OK")

# Check if the new readline_with_timeout is available
from go_analysis.analyzer.process import _readline_with_timeout
print(f"_readline_with_timeout: OK")

# Check WindowsAnalyzer has the new analyze()
from go_analysis.analyzer.windows import WindowsAnalyzer
import inspect
src = inspect.getsource(WindowsAnalyzer.analyze)
if "moves_to_katago_format" in src:
    print("WindowsAnalyzer.analyze uses moves_to_katago_format: OK")
else:
    print("WARNING: WindowsAnalyzer.analyze DOES NOT use moves_to_katago_format")

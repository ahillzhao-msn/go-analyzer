@echo off
REM Start Coordinator — Go Analyzer v0.4.4
REM Path-independent: uses %~dp0 for current directory.
cd /d "%~dp0"
start "" "%~dp0venv\Scripts\pythonw.exe" -m go_analysis.distributed.coordinator ^
    --sgf-dir training --store-dir analysis_store ^
    --port 18081 --data-dir coordinator_data --log-level INFO
echo Coordinator starting on :18081

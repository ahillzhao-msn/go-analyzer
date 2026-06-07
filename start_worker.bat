@echo off
REM Start Worker — Go Analyzer v0.4.4
REM Path-independent: uses %~dp0 for current directory.
cd /d "%~dp0"
start "" "%~dp0venv\Scripts\pythonw.exe" -m go_analysis.distributed.worker ^
    --sgf-dir training --store-dir analysis_store ^
    --katago "%~dp0katago\katago.exe" ^
    --model "%~dp0models\kata1-b18c384nbt-s6582191360-d3422816034.bin.gz" ^
    --config "%~dp0analysis_config.cfg" ^
    --visits 25 --sync-port 18083 --coordinator-url http://127.0.0.1:18081 ^
    --log-dir logs --log-level INFO --katago-max-games 50 --katago-max-age 1800
echo Worker starting on :18083

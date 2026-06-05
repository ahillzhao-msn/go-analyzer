@echo off
REM ============================================================
REM start_worker.bat — 启动 Worker (后台 pythonw, KataGo 常驻)
REM v0.4.0
REM KataGo 每 50 局或 30 分钟重启一次（兼顾效率与防死锁）
REM ============================================================
cd /d "%~dp0"

set SGF_DIR=training
set STORE_DIR=analysis_store
set KATAGO=katago\katago.exe
set MODEL=models\kata1-b18c384nbt-s6582191360-d3422816034.bin.gz
set CONFIG=analysis_config.cfg
set VISITS=25
set SYNC_PORT=18083
set LOG_DIR=logs
set COORD_URL=http://127.0.0.1:18081

start "" /B pythonw -m go_analysis.distributed.worker ^
    --sgf-dir %SGF_DIR% ^
    --store-dir %STORE_DIR% ^
    --katago %KATAGO% ^
    --model %MODEL% ^
    --config %CONFIG% ^
    --visits %VISITS% ^
    --sync-port %SYNC_PORT% ^
    --coordinator-url %COORD_URL% ^
    --log-dir %LOG_DIR% ^
    --log-level INFO ^
    --katago-max-games 50 ^
    --katago-max-age 1800

echo Worker started (KataGo persistent, restarts every 50 games)
echo SGF: %SGF_DIR%
echo Store: %STORE_DIR%
echo Log: %LOG_DIR%\worker.log
echo Status: http://localhost:%SYNC_PORT%/status

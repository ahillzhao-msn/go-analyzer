@echo off
REM ============================================================
REM start_worker.bat — 启动 Worker (后台 pythonw)
REM v0.4.0
REM 用法: 双击运行, 或通过 SCHTASKS 调度
REM ============================================================
cd /d "%~dp0"

REM 配置
set SGF_DIR=training
set STORE_DIR=analysis_store
set KATAGO=katago\katago.exe
set MODEL=models\kata1-b18c384nbt-s6582191360-d3422816034.bin.gz
set CONFIG=analysis_config.cfg
set VISITS=25
set SYNC_PORT=18083
set LOG_DIR=logs

REM Coordinator 地址 — 改为本地 coordinator (127.0.0.1)
set COORD_URL=http://127.0.0.1:18081

REM 启动 Worker (无控制台窗口)
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
    --log-level INFO

echo Worker started
echo SGF: %SGF_DIR%
echo Store: %STORE_DIR%
echo Log: %LOG_DIR%\worker.log
echo Status: http://localhost:%SYNC_PORT%/status

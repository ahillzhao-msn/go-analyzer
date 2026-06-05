@echo off
REM ============================================================
REM start_coordinator.bat — 启动 Coordinator (后台 pythonw)
REM v0.4.0
REM 用法: 双击运行, 或从命令行 start /B start_coordinator.bat
REM ============================================================
cd /d "%~dp0"

REM 配置
set DATA_DIR=coordinator_data
set LOG_DIR=%DATA_DIR%
set PORT=18081
set PEER_URL=

REM 如果是备份节点, 设置 --peer 指向主节点
REM set PEER_URL=--peer http://192.168.9.32:18081

REM 启动 Coordinator (无控制台窗口)
start "" /B pythonw -m go_analysis.distributed.coordinator ^
    --sgf-dir training ^
    --store-dir analysis_store ^
    --port %PORT% ^
    --data-dir %DATA_DIR% ^
    --log-level INFO ^
    %PEER_URL%

echo Coordinator started on port %PORT%
echo Data dir: %DATA_DIR%
echo Log: %LOG_DIR%\coordinator.log

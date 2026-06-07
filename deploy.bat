@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM go-analyzer deploy — 统一部署脚本 v0.4.4
REM 用法:  deploy <command>
REM 命令:
REM   install   安装 wheel（不启停服务）
REM   stop      停止所有服务
REM   start     启动 Coordinator + Worker
REM   full      完整部署 = stop + install + start（默认）
REM   status    查看运行状态
REM   build     从源码重建 wheel
REM 所有路径基于 %~dp0，可在任意目录运行。
REM ============================================================

set "PROJ_DIR=%~dp0"
cd /d "%PROJ_DIR%"

set "VENV_PYTHON=%PROJ_DIR%venv\Scripts\python.exe"
set "VENV_PYTHONW=%PROJ_DIR%venv\Scripts\pythonw.exe"

if "%1"=="" set "CMD=full" else set "CMD=%1"

if /i "%CMD%"=="install" goto :INSTALL
if /i "%CMD%"=="build"   goto :BUILD
if /i "%CMD%"=="stop"    goto :STOP
if /i "%CMD%"=="start"   goto :START
if /i "%CMD%"=="full"    goto :FULL
if /i "%CMD%"=="status"  goto :STATUS
echo Unknown command: %CMD%
echo Usage: %~nx0 ^<install^|stop^|start^|full^|status^|build^>
exit /b 1

:BUILD
echo [1/3] Building wheel from source...
"%VENV_PYTHON%" -m pip install build -q >nul 2>&1
"%VENV_PYTHON%" -m build --wheel -q >nul 2>&1
if %errorlevel% neq 0 ( echo FAILED & exit /b 1 )
for %%w in ("%PROJ_DIR%dist\go_analyzer-*-py3-none-any.whl") do set "WHEEL=%%w"
copy /y "!WHEEL!" "%PROJ_DIR%" >nul
echo OK & exit /b 0

:INSTALL
echo [1/2] Installing wheel...
for %%w in ("%PROJ_DIR%go_analyzer-*-py3-none-any.whl") do set "WHEEL=%%w"
if not exist "%WHEEL%" ( echo No wheel found. Run 'deploy build' first. & exit /b 1 )
"%VENV_PYTHON%" -m pip install --force-reinstall --no-deps "%WHEEL%" >nul 2>&1
if %errorlevel% neq 0 ( echo FAILED & exit /b 1 )
echo OK
echo [2/2] Clearing stale coordinator DB...
del /q "%PROJ_DIR%coordinator_data\coordinator.db" 2>nul
echo OK & exit /b 0

:STOP
echo Stopping services...
taskkill /f /im pythonw.exe >nul 2>nul
taskkill /f /im katago.exe >nul 2>nul
echo OK & exit /b 0

:START
echo [1/4] Waiting for port 18081 to free up...
:WAIT_PORT
netstat -ano | findstr ":18081 " >nul 2>nul
if !errorlevel! equ 0 (
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":18081 " ^| findstr LISTENING') do (
        taskkill /f /pid %%p >nul 2>nul
    )
    timeout /t 3 /nobreak >nul
    goto WAIT_PORT
)
echo   Port 18081 free
echo [2/4] Clearing old logs...
del /q "%PROJ_DIR%logs\worker.log" 2>nul
del /q "%PROJ_DIR%coordinator_data\coordinator.log" 2>nul
echo [3/4] Starting Coordinator (port 18081)...
start "" "%VENV_PYTHONW%" -m go_analysis.distributed.coordinator ^
    --sgf-dir training --store-dir analysis_store ^
    --port 18081 --data-dir coordinator_data --log-level INFO
if errorlevel 1 ( echo FAILED & exit /b 1 )
timeout /t 4 /nobreak >nul
echo [4/4] Starting Worker (port 18083)...
start "" "%VENV_PYTHONW%" -m go_analysis.distributed.worker ^
    --sgf-dir training --store-dir analysis_store ^
    --katago "%PROJ_DIR%katago\katago.exe" ^
    --model "%PROJ_DIR%models\kata1-b18c384nbt-s6582191360-d3422816034.bin.gz" ^
    --config "%PROJ_DIR%analysis_config.cfg" ^
    --visits 25 --sync-port 18083 ^
    --coordinator-url http://127.0.0.1:18081 ^
    --log-dir logs --log-level INFO ^
    --katago-max-games 50 --katago-max-age 1800
if errorlevel 1 ( echo FAILED & exit /b 1 )
timeout /t 3 /nobreak >nul
echo OK
echo Services started: http://localhost:18081/stats
exit /b 0

:FULL
call :STOP
call :INSTALL
call :START
echo ============================================
echo  Go Analyzer v0.4.4 DEPLOYED
echo ============================================
echo  Dir:  %PROJ_DIR%
echo  Stats: http://localhost:18081/stats
echo  Log:  %PROJ_DIR%logs\worker.log
echo  Stop: %~nx0 stop
echo ============================================
exit /b 0

:STATUS
echo === Processes ===
tasklist /fi "IMAGENAME eq pythonw.exe" 2>nul
tasklist /fi "IMAGENAME eq katago.exe" 2>nul
echo === Ports ===
netstat -ano | findstr "1808"
echo === Coordinator log (last 10) ===
if exist "%PROJ_DIR%coordinator_data\coordinator.log" (
    powershell -Command "Get-Content '%PROJ_DIR%coordinator_data\coordinator.log' -Tail 10" 2>nul
)
echo === Worker log (last 5) ===
if exist "%PROJ_DIR%logs\worker.log" (
    powershell -Command "Get-Content '%PROJ_DIR%logs\worker.log' -Tail 5" 2>nul
)
exit /b 0

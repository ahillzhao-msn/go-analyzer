@echo off
chcp 65001 >nul
cd /d "%~dp0"

set CMD=%~1
if "%CMD%"=="" set CMD=full

if /i "%CMD%"=="stop" goto :STOP
if /i "%CMD%"=="start" goto :START
if /i "%CMD%"=="install" goto :INSTALL
if /i "%CMD%"=="build" goto :BUILD
if /i "%CMD%"=="status" goto :STATUS
if /i "%CMD%"=="katago" goto :KATAGO
if /i "%CMD%"=="full" goto :FULL
echo Usage: %~nx0 ^<stop^|start^|install^|build^|status^|katago^|full^>
exit /b 1

:STOP
echo Stopping services...
taskkill /f /im pythonw.exe >nul 2>nul
exit /b 0

:BUILD
echo Building wheel...
"%~dp0venv\Scripts\python.exe" -m pip install build -q >nul 2>&1
"%~dp0venv\Scripts\python.exe" -m build --wheel -q >nul 2>&1
if errorlevel 1 echo FAILED & exit /b 1
copy /y "%~dp0dist\go_analyzer-*-py3-none-any.whl" "%~dp0" >nul
echo OK
exit /b 0

:INSTALL
echo Installing wheel...
set WHEEL=
for %%f in ("%~dp0go_analyzer-*-py3-none-any.whl") do set WHEEL=%%f
if "%WHEEL%"=="" echo No wheel found & exit /b 1
"%~dp0venv\Scripts\python.exe" -m pip install --force-reinstall --no-deps "%WHEEL%" >nul 2>&1
if errorlevel 1 echo FAILED & exit /b 1
del /q "%~dp0coordinator_data\coordinator.db" 2>nul
echo OK
exit /b 0

:KATAGO
echo Downloading katago.exe (OpenCL, v1.16.5-trunk)...
mkdir "%~dp0katago" 2>nul
"%~dp0venv\Scripts\python.exe" -c "from go_analysis.analyzer.batch_adapter import download_katago; print(download_katago(r'%~dp0katago'))"
if not exist "%~dp0katago\katago.exe" echo FAILED & exit /b 1
echo OK
exit /b 0

:START
call :KILL_PORT 18081
echo Starting Coordinator...
start "" "%~dp0venv\Scripts\pythonw.exe" -m go_analysis.distributed.coordinator --sgf-dir training --store-dir analysis_store --port 18081 --data-dir coordinator_data --log-level INFO
timeout /t 5 /nobreak >nul

echo Starting Worker (batch_analysis mode)...
start "" "%~dp0venv\Scripts\pythonw.exe" -m go_analysis.distributed.worker ^
    --sgf-dir training --store-dir analysis_store ^
    --katago "%~dp0katago\katago.exe" ^
    --model "%~dp0models\kata1-b18c384nbt-s6582191360-d3422816034.bin.gz" ^
    --human-model "%~dp0models\b18c384nbt-humanv0.bin.gz" ^
    --config "%~dp0analysis_config.cfg" ^
    --visits 25 --sync-port 18083 --coordinator-url http://127.0.0.1:18081 ^
    --log-dir logs --log-level INFO --batch-mode
timeout /t 3 /nobreak >nul
echo OK
exit /b 0

:FULL
call :STOP
call :INSTALL
call :KATAGO
del /q "%~dp0logs\worker.log" 2>nul
del /q "%~dp0coordinator_data\coordinator.log" 2>nul
call :START
echo ======== Done ========
echo Dir: %~dp0
echo Stats: http://localhost:18081/stats
echo Stop: %~nx0 stop
exit /b 0

:STATUS
echo === Processes ===
tasklist /fi "IMAGENAME eq pythonw.exe" 2>nul
echo === Ports ===
netstat -ano | findstr 1808
exit /b 0

:KILL_PORT
set PORT=%1
set RETRY=0
:KILL_LOOP
set /a RETRY+=1
if %RETRY% gtr 10 (
    echo   Port %PORT% still occupied, continuing anyway...
    exit /b 0
)
netstat -ano | findstr ":%PORT%" >nul 2>nul
if errorlevel 1 exit /b 0
echo   Port %PORT% in use, killing...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr LISTENING') do (
    taskkill /f /t /pid %%p >nul 2>nul
    powershell -Command "Stop-Process -Id %%p -Force -ErrorAction SilentlyContinue" >nul 2>nul
    wmic process where ProcessId=%%p delete >nul 2>nul
)
timeout /t 3 /nobreak >nul
goto KILL_LOOP

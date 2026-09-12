@echo off
rem ============================================================
rem  DocKit quick launcher (dev mode)
rem  First run compiles Rust (1-3 min); later runs are fast.
rem  Requires: Node.js + Rust + uv (project .venv via uv sync)
rem ============================================================
setlocal
cd /d "%~dp0"

rem 1. frontend deps
if not exist "node_modules" (
    echo [1/3] Installing frontend dependencies...
    call npm install
    if errorlevel 1 goto :fail
) else (
    echo [1/3] Frontend dependencies ready
)

rem 2. python backend env (Tauri sidecar needs it)
if not exist ".venv\Scripts\python.exe" (
    echo [2/3] Setting up Python environment...
    call uv sync
    if errorlevel 1 goto :fail
) else (
    echo [2/3] Python environment ready
)

rem Record actual launcher runs; native code logs timings only, no task data.
if not exist ".tmp_t" mkdir ".tmp_t"
if not defined DOCKIT_STARTUP_TRACE set "DOCKIT_STARTUP_TRACE=%CD%\.tmp_t\startup-launch.jsonl"

rem 3. launch Tauri (dev mode: starts vite, compiles, opens window)
echo [3/3] Launching DocKit... first Rust compile takes 1-3 minutes.
echo Note: keep this window open while using the app; closing it quits.
echo.
call npm run tauri dev
echo.
echo DocKit exited (code %errorlevel%).
if errorlevel 1 (
    echo Startup failed. See errors above, or run "npm run tauri dev" manually.
)
pause
endlocal
exit /b 0

:fail
echo.
echo Startup FAILED. Check the errors above.
echo Make sure Node.js / Rust / uv are installed and on PATH.
pause
endlocal
exit /b 1

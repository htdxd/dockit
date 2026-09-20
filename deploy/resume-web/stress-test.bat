@echo off
setlocal
cd /d "%~dp0"
set "PYTHONHOME=%~dp0runtime\python"
set "PYTHONPATH=%~dp0runtime\backend"
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
set "PATH=%~dp0runtime\tools;%~dp0runtime\poppler\bin;%PATH%"
"%~dp0runtime\python\python.exe" -m skill_toolbox.web.loadtest %*
if errorlevel 1 pause
endlocal

@echo off
rem ============================================================
rem  DocKit 一键启动（开发模式）
rem  首次启动会编译 Rust（约 1-3 分钟），之后增量编译很快。
rem  前置要求：Node.js + Rust + 项目 .venv（uv sync 已执行）
rem ============================================================
setlocal
cd /d "%~dp0"

rem 1. 检查前端依赖
if not exist node_modules (
    echo [1/3] 安装前端依赖...
    call npm install || goto :fail
) else (
    echo [1/3] 前端依赖已就绪
)

rem 2. 检查 Python 后端环境（Tauri 侧边车需要）
if not exist .venv\Scripts\python.exe (
    echo [2/3] 初始化 Python 环境...
    uv sync || goto :fail
) else (
    echo [2/3] Python 环境已就绪
)

rem 3. 启动 Tauri（dev 模式：自动拉起 vite + 编译 + 打开窗口）
echo [3/3] 启动 DocKit（首次编译 Rust 需 1-3 分钟，请耐心等待）...
call npm run tauri dev
goto :end

:fail
echo.
echo 启动失败：请检查上方报错，确认 Node.js / Rust / uv 已安装。
pause
exit /b 1

:end
endlocal

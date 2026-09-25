@echo off
setlocal
cd /d "%~dp0"

set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"

if not defined PYEXE (
    if exist "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
        set "PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
    )
)

if not defined PYEXE where python >nul 2>nul && set "PYEXE=python"

if not defined PYEXE (
    echo.
    echo [错误] 未找到 Python 解释器。
    echo 请安装 Python 3.9 及以上版本，或在项目目录下创建 .venv 虚拟环境。
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0app.py" (
    echo.
    echo [错误] 当前目录下未找到 app.py，请确认项目文件完整。
    echo.
    pause
    exit /b 1
)

echo 正在启动 Windows 进程管理工具 ...
echo.
"%PYEXE%" app.py %*
set "RC=%errorlevel%"

if not "%RC%"=="0" (
    echo.
    echo [错误] 程序异常退出，错误码 %RC%。
)

echo.
echo 程序已退出，按任意键关闭窗口。
pause >nul
endlocal

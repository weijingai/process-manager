@echo off
REM ==========================================================
REM  Windows 进程管理工具 - 打包【桌面版】exe（原生窗口，无需浏览器）
REM  本文件为 GBK 编码，修改时请勿另存为 UTF-8
REM  桌面版依赖 tkinter，需使用官方 Python 安装包（自带 Tk），
REM  精简版 / 嵌入式 Python 可能没有 tkinter。
REM ==========================================================
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"

if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)

if not defined PY (
  if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
)

if not defined PY (
  echo [错误] 未找到 Python，请先安装 Python 3.9 及以上版本并勾选 Add to PATH。
  echo 下载地址: https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

echo 使用解释器: %PY%

%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo [错误] 当前 Python 没有 tkinter，无法打包桌面版。
  echo 请改用 python.org 官方安装包重新安装 Python，安装时勾选
  echo   - "Add python.exe to PATH"
  echo   - "tcl/tk and IDLE"
  echo 官方 Python 自带 Tk；精简版、嵌入式版一般不含 tkinter。
  pause
  exit /b 1
)

%PY% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo 正在安装打包依赖 PyInstaller ...
  %PY% -m pip install pyinstaller
  if errorlevel 1 (
    echo [错误] PyInstaller 安装失败。
    pause
    exit /b 1
  )
)

%PY% -c "import psutil" >nul 2>nul
if errorlevel 1 (
  echo 正在安装运行依赖 psutil ...
  %PY% -m pip install psutil
  if errorlevel 1 (
    echo [错误] psutil 安装失败。
    pause
    exit /b 1
  )
)

echo.
echo 正在打包桌面版，首次执行需要 2-4 分钟，请稍候 ...
%PY% -m PyInstaller build-gui.spec --noconfirm --clean
if errorlevel 1 (
  echo [错误] 打包失败，请查看上方输出。
  pause
  exit /b 1
)

echo.
echo ==========================================================
echo  桌面版打包完成: dist\WindowsProcessManager-Desktop.exe
echo  双击即可打开原生窗口，不需要浏览器、不需要 Python。
echo ==========================================================
pause

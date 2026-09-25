@echo off
REM ==========================================================
REM  Windows 进程管理工具 - 打包【Web 版】exe（浏览器界面）
REM  本文件为 GBK 编码，修改时请勿另存为 UTF-8
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
echo 正在打包 Web 版，首次执行需要 1-3 分钟，请稍候 ...
%PY% -m PyInstaller build.spec --noconfirm --clean
if errorlevel 1 (
  echo [错误] 打包失败，请查看上方输出。
  pause
  exit /b 1
)

echo.
echo ==========================================================
echo  Web 版打包完成: dist\WindowsProcessManager.exe
echo  双击运行后会用浏览器打开管理界面。
echo  如需【桌面版】（不依赖浏览器的原生窗口），请运行 build-gui.bat
echo ==========================================================
pause

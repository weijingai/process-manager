@echo off
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if not errorlevel 1 goto HASADMIN

echo 正在申请管理员权限，请在弹出的 UAC 窗口中点击"是"。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0elevate.ps1"
if errorlevel 1 (
    echo.
    echo [错误] 提权失败。请右键 start.bat 选择"以管理员身份运行"。
    pause
)
goto :eof

:HASADMIN
echo 已具备管理员权限，正在启动。
call "%~dp0start.bat" %*

endlocal

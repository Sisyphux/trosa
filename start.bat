@echo off
chcp 65001 >nul
title CRM Reminder System
cd /d "%~dp0"

rem API 密钥和登录密码请保存于 .env；不要写入启动脚本或提交到 Git。

set "PY=%CD%\venv\Scripts\python.exe"

if not exist "%PY%" (
    echo Creating virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (echo Failed & pause & exit /b 1)
    echo Installing dependencies...
    "%CD%\venv\Scripts\python.exe" -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo Install failed, please run manually:
        echo   %CD%\venv\Scripts\python.exe -m pip install -r requirements.txt
        pause & exit /b 1
    )
)

echo.
echo ============================
echo   CRM Reminder System
echo ============================
echo.
echo Starting server...
echo Browser will open automatically
echo Press Ctrl+C to stop
echo.

start "" /B "%PY%" app.py > "%TEMP%\crm_server.log" 2>&1

set "READY="
for /l %%i in (1,1,15) do (
    >nul 2>&1 powershell -NoProfile -Command "try{$c=New-Object System.Net.Sockets.TcpClient;$c.Connect('127.0.0.1',8080);$c.Close();$true}catch{$false}" && set READY=1
    if defined READY goto ready
    >nul ping -n 2 127.0.0.1
)
:ready

if defined READY (
    echo Server started, opening browser...
    start http://localhost:8080
) else (
    echo Warning: server may not be ready, please visit http://localhost:8080 manually
    start http://localhost:8080
)

"%PY%" -c "import sys; sys.stdin.read()" 2>nul || pause

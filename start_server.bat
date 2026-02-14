@echo off
title AI Reasoning Agent Server
echo ==============================================
echo    Starting AI Reasoning Agent Server...
echo ==============================================
echo.

cd /d "%~dp0"
echo [INFO] Project root: %CD%
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    pause
    exit /b 1
)

if not exist venv\Scripts\python.exe (
    echo [INFO] No venv found - creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [INFO] Virtual environment created.
    echo.
)

set VENV_PIP=venv\Scripts\pip.exe
set VENV_WAITRESS=venv\Scripts\waitress-serve.exe

if exist requirements.txt (
    echo [INFO] Installing requirements...
    %VENV_PIP% install -r requirements.txt --no-deps --quiet
    if errorlevel 1 (
        echo [ERROR] pip install failed.
        pause
        exit /b 1
    )
    echo [INFO] Requirements installed.
)

if not exist %VENV_WAITRESS% (
    echo [INFO] Installing Waitress...
    %VENV_PIP% install waitress
)

if not exist .env (
    echo [WARNING] .env file not found.
)

echo.
echo ==============================================
echo  SERVER RUNNING on http://0.0.0.0:5008
echo  Press CTRL+C to stop
echo ==============================================
echo.

%VENV_WAITRESS% --host=0.0.0.0 --port=5008 app:app

pause

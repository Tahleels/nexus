@echo off
title AI Reasoning Agent Server
echo ==============================================
echo    Starting AI Reasoning Agent Server...
echo ==============================================
echo.

:: Always work from the PROJECT ROOT (one level up from this scripts\ folder)
cd /d "%~dp0.."
echo [INFO] Project root: %CD%
echo.

:: ── Python check ──────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo         Download Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: ── Virtual environment ───────────────────────────────────────────────────────
if not exist venv\Scripts\activate.bat (
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

echo [INFO] Activating virtual environment...
call venv\Scripts\activate.bat

:: ── Install / update dependencies ─────────────────────────────────────────────
if exist requirements.txt (
    echo [INFO] Installing requirements (this may take a few minutes on first run)...
    pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo [ERROR] pip install failed. Check requirements.txt and your internet connection.
        pause
        exit /b 1
    )
    echo [INFO] Requirements installed.
) else (
    echo [WARNING] requirements.txt not found - skipping dependency install.
)

:: ── Waitress check ────────────────────────────────────────────────────────────
python -c "import waitress" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing Waitress WSGI server...
    pip install waitress
)

:: ── .env check ────────────────────────────────────────────────────────────────
if not exist .env (
    echo.
    echo [WARNING] .env file not found!
    echo           Create a .env file in %CD% with your API keys and DB settings.
    echo           The server will start but features requiring config will fail.
    echo.
)

:: ── Launch ────────────────────────────────────────────────────────────────────
echo.
echo ==============================================
echo  SERVER IS RUNNING  ^|  Press CTRL+C to stop
echo  Listening on  http://0.0.0.0:5008
echo ==============================================
echo.

waitress-serve --host=0.0.0.0 --port=5008 app:app

pause

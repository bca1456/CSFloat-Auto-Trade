@echo off
setlocal

title CSFloat Auto Trade - Launcher
cls

echo ========================================
echo   CSFLOAT AUTO TRADE
echo ========================================
echo.

REM Step 1: Check Python
echo [1/3] Checking Python...

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [X] Python is not installed!
    echo     Download Python 3.10+ from: https://www.python.org/downloads/
    echo     Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo [OK] Python %PYTHON_VERSION%
echo.

REM Step 2: Check dependencies
echo [2/3] Checking dependencies...

set MISSING=0

python -c "import aiohttp" >nul 2>&1
if %errorlevel% neq 0 (
    echo [X] aiohttp is not installed
    set MISSING=1
) else (
    echo [OK] aiohttp
)

python -c "import aiosteampy" >nul 2>&1
if %errorlevel% neq 0 (
    echo [X] aiosteampy is not installed
    set MISSING=1
) else (
    echo [OK] aiosteampy
)

echo.

if %MISSING%==1 (
    echo Installing missing dependencies...
    python -m pip install --upgrade pip >nul 2>&1
    python -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo.
        echo [X] Failed to install dependencies
        echo     1. Check your internet connection
        echo     2. Try running as Administrator
        pause
        exit /b 1
    )
    echo.
    echo [OK] Dependencies installed
    echo.
)

REM Step 3: Launch
echo [3/3] Launching CSFloat Auto Trade...
timeout /t 1 /nobreak >nul

start "" py CSFloat_Auto_Trade.py

timeout /t 5 /nobreak >nul
exit

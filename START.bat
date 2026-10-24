@echo off
setlocal enabledelayedexpansion
title eCommerce Scraper Pro
color 0A

cls
echo.
echo  =====================================================
echo    eCommerce Scraper Pro  ^|  Windows Launcher
echo  =====================================================
echo.

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 1: Find Python 3.8+
:: (Tries specific install paths first to avoid Anaconda/conda conflicts)
:: ─────────────────────────────────────────────────────────────────────────────
set "PYTHON="

for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
    "%PROGRAMFILES%\Python313\python.exe"
    "%PROGRAMFILES%\Python312\python.exe"
    "%PROGRAMFILES%\Python311\python.exe"
    "%PROGRAMFILES%\Python310\python.exe"
    "%PROGRAMFILES(x86)%\Python313\python.exe"
    "%PROGRAMFILES(x86)%\Python312\python.exe"
    "%PROGRAMFILES(x86)%\Python311\python.exe"
    "%PROGRAMFILES(x86)%\Python310\python.exe"
) do (
    if "!PYTHON!"=="" (
        if exist %%~P (
            %%~P -c "import sys; exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
            if !errorlevel! == 0 set "PYTHON=%%~P"
        )
    )
)

:: Try Windows py launcher
if "!PYTHON!"=="" (
    py -3 -c "import sys; exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
    if !errorlevel! == 0 set "PYTHON=py -3"
)

:: Last resort: plain python (skip if it's conda/anaconda)
if "!PYTHON!"=="" (
    python -c "import sys; ok=sys.version_info>=(3,8) and 'conda' not in sys.version.lower() and 'anaconda' not in sys.version.lower(); exit(0 if ok else 1)" >nul 2>&1
    if !errorlevel! == 0 set "PYTHON=python"
)

:: ── Python not found — download it ───────────────────────────────────────────
if "!PYTHON!"=="" (
    echo  [!] Python 3.8+ was not found on this computer.
    echo.
    echo  Installing Python automatically...
    echo  A browser window will open the Python installer.
    echo.
    echo  IMPORTANT when installing:
    echo    ^> CHECK "Add python.exe to PATH"
    echo    ^> Click "Install Now"
    echo.
    echo  After Python installs, close this window
    echo  and double-click START.bat again.
    echo.
    start "" "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    pause
    exit /b 1
)

for /f "tokens=*" %%V in ('!PYTHON! --version 2^>^&1') do set PY_VER=%%V
echo  [OK] Found: !PY_VER!
echo.

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 2: Upgrade pip silently
:: ─────────────────────────────────────────────────────────────────────────────
!PYTHON! -m pip install --upgrade pip --quiet --disable-pip-version-check >nul 2>&1

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 3: Install all required packages
:: ─────────────────────────────────────────────────────────────────────────────
echo  [..] Checking and installing required packages...
echo       (First time: ~1-2 minutes. Next time: instant.)
echo.

!PYTHON! -m pip install ^
    "fastapi==0.115.6" ^
    "uvicorn[standard]==0.34.0" ^
    "httpx==0.28.1" ^
    "sqlalchemy==2.0.36" ^
    "aiofiles==24.1.0" ^
    "python-multipart==0.0.12" ^
    "beautifulsoup4==4.12.2" ^
    --quiet --disable-pip-version-check

if !errorlevel! neq 0 (
    echo  [WARN] Normal install failed. Trying with --user flag...
    !PYTHON! -m pip install ^
        "fastapi==0.115.6" ^
        "uvicorn[standard]==0.34.0" ^
        "httpx==0.28.1" ^
        "sqlalchemy==2.0.36" ^
        "aiofiles==24.1.0" ^
        "python-multipart==0.0.12" ^
        "beautifulsoup4==4.12.2" ^
        --user --quiet --disable-pip-version-check

    if !errorlevel! neq 0 (
        echo.
        echo  [ERROR] Could not install packages.
        echo  ^ Check your internet connection and try again.
        echo  ^ If problem persists, run as Administrator.
        pause
        exit /b 1
    )
)

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 4: Verify imports work
:: ─────────────────────────────────────────────────────────────────────────────
!PYTHON! -c "import fastapi, uvicorn, httpx, sqlalchemy, aiofiles" >nul 2>&1
if !errorlevel! neq 0 (
    echo  [ERROR] Package verification failed.
    echo  Please right-click START.bat and "Run as Administrator".
    pause
    exit /b 1
)

echo  [OK] All packages ready.
echo.

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 5: Create required folders
:: ─────────────────────────────────────────────────────────────────────────────
if not exist "images" mkdir images >nul 2>&1
if not exist "static" mkdir static >nul 2>&1

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 6: Find a free port (try 8000, 8001, 8002)
:: ─────────────────────────────────────────────────────────────────────────────
set PORT=8000
netstat -ano 2>nul | findstr /C:":8000 " | findstr "LISTENING" >nul 2>&1
if !errorlevel! == 0 (
    set PORT=8001
    netstat -ano 2>nul | findstr /C:":8001 " | findstr "LISTENING" >nul 2>&1
    if !errorlevel! == 0 set PORT=8002
)

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 7: Open browser after 4 seconds, then start server
:: ─────────────────────────────────────────────────────────────────────────────
echo  =====================================================
echo    Ready!  Opening: http://localhost:!PORT!
echo    Browser opens in 4 seconds automatically.
echo.
echo    To STOP the server: close this window.
echo  =====================================================
echo.

start "" cmd /c "ping -n 5 127.0.0.1 >nul && start http://localhost:!PORT!"

!PYTHON! -m uvicorn main:app --host 0.0.0.0 --port !PORT!

echo.
echo  Server stopped. Press any key to close.
pause >nul

@echo off
cls
echo ============================================================
echo   POCKET YUME -- Launcher
echo   AI Subtitles for Japanese Videos
echo ============================================================
echo.

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found!
    echo Install Python 3.10+ from python.org
    echo Make sure "Add to PATH" is checked during install.
    echo.
    pause
    exit /b 1
)

REM Check script exists
if not exist "pocket_yume.py" (
    echo ERROR: pocket_yume.py not found!
    echo Run this from the Yume folder.
    echo.
    pause
    exit /b 1
)

REM Launch
python pocket_yume.py %*

REM If exited
echo.
echo Yume stopped.
pause

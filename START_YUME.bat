@echo off
REM Always run from the folder this script lives in — double-clicking via
REM "Run as administrator" starts in System32 and would break the launch.
cd /d "%~dp0"
cls
echo ============================================================
echo   POCKET YUME -- Launcher
echo   Local AI subtitles for videos
echo ============================================================
echo.

REM Prefer a virtual environment that actually has Yume installed (the setup
REM wizard recommends "yume-env"): packages installed there are invisible to
REM the system Python. An empty venv is skipped.
for %%V in (yume-env venv .venv) do (
    if exist "%%V\Scripts\python.exe" (
        "%%V\Scripts\python.exe" -c "import importlib.util,sys; sys.exit(importlib.util.find_spec('faster_whisper') is None)" >nul 2>&1
        if not errorlevel 1 (
            set "PY=%%V\Scripts\python.exe"
            goto :run
        )
    )
)

REM Find Python: try "python" first, then the "py" launcher, which exists
REM even when "Add to PATH" was left unchecked during installation.
set "PY=python"
python --version >nul 2>&1
if %errorlevel% equ 0 goto :run
py -3 --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY=py -3"
    goto :run
)

echo ERROR: Python not found!
echo.
echo   1. Download Python from: https://www.python.org/downloads/
echo   2. During install, CHECK the box "Add python.exe to PATH"
echo   3. Run this launcher again
echo.
set /p OPEN="Open the Python download page now? [Y/n] "
if /i not "%OPEN%"=="n" start "" "https://www.python.org/downloads/"
pause
exit /b 1

:run
if not exist "pocket_yume.py" (
    echo ERROR: pocket_yume.py not found!
    echo This launcher must stay inside the Yume folder.
    echo.
    pause
    exit /b 1
)

%PY% pocket_yume.py %*

echo.
echo Yume stopped.
pause

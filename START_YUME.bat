@echo off
REM Always run from the folder this script lives in — double-clicking via
REM "Run as administrator" starts in System32 and would break the launch.
cd /d "%~dp0"
cls
echo ============================================================
echo   POCKET YUME -- Launcher
echo   AI Subtitles for Japanese Videos
echo ============================================================
echo.

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

@echo off
rem ASCII only on purpose - see install.py for why.
rem cmd.exe re-reads this file by byte offset while running it, so non-ASCII
rem text (or a mid-file chcp) desynchronises the parser and turns echo lines
rem into commands. All messages are printed from Python instead.

cd /d "%~dp0"

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"

if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)

if not defined PY (
    echo.
    echo   [x] Python not found. Install Python 3.9+ and tick
    echo       "Add Python to PATH", or install the py launcher.
    echo.
    pause
    exit /b 1
)

%PY% "%~dp0install.py"
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%

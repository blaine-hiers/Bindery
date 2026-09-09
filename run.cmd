@echo off
rem Double-click this. It finds Python and opens Bindery in your browser.
rem Nothing to install --- the standard library is the whole dependency list.
rem
rem   run.cmd -v     say what it is doing

setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (
    py "delivery\bindery\app.py" %*
    goto done
)

where python >nul 2>&1
if %errorlevel%==0 (
    python "delivery\bindery\app.py" %*
    goto done
)

echo.
echo   Python was not found on this machine.
echo   Install it from https://www.python.org/downloads/ and tick
echo   "Add python.exe to PATH" on the first screen of the installer.
echo.
pause

:done
endlocal

@echo off
REM start.bat - run the SN902W status-light daemon in this console window.
REM Double-click this file (or run it in your own terminal). Keep the window
REM open - closing it stops the daemon and the light stops reacting.
cd /d "%~dp0"
echo ============================================================
echo   SN902W status light daemon
echo   web console -^> http://127.0.0.1:7800
echo   hook server -^> 127.0.0.1:47800
echo   close this window to stop
echo ============================================================
echo.
python webserver.py --no-browser
pause

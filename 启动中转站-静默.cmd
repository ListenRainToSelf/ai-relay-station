@echo off
rem ===========================================================================
rem  Local AI Relay - Windows silent launcher (tray only, no console window)
rem
rem  Starts with pythonw: no console window, just the tray icon.
rem  Quit via the tray icon menu. Logs go to <data dir>\logsirelay.log.
rem  Good for the Startup folder or install-windows.ps1 -Autostart.
rem
rem  NOTE: keep this file ASCII-only + CRLF (see the other launcher's note).
rem ===========================================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYW="
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not defined PYW (
  for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    set "PYW=%%i"
    goto :found
  )
)
:found
if not defined PYW (
  echo   [ERROR] pythonw.exe not found. Install Python 3.10+ or create a venv,
  echo           or use the launcher that keeps a console window.
  pause
  exit /b 1
)

start "" "%PYW%" -m airelay --no-window %*
endlocal

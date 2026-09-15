@echo off
rem ===========================================================================
rem  Local AI Relay - Windows launcher (tray + console window + visible logs)
rem
rem  Double-click to start. It checks dependencies, then starts in desktop mode:
rem  a tray icon appears and the console window opens (logs print here too).
rem  Closing this window stops the service. For a silent tray-only run use the
rem  other launcher (-silent). Extra args are passed through, e.g.
rem      <this script> --port 8090 --host 0.0.0.0
rem
rem  NOTE: keep this file ASCII-only + CRLF. cmd.exe parses batch files with the
rem  OEM code page; UTF-8 Chinese text can eat the CR at end of line and make the
rem  next line run as a command. See README for the Chinese documentation.
rem ===========================================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Local AI Relay

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo   [ERROR] Python not found.
  echo           Install Python 3.10+ and tick "Add python.exe to PATH",
  echo           or create a venv first:  python -m venv .venv
  echo.
  pause
  exit /b 1
)

rem ---- dependency self-check: install what is missing (slow on first run) ----
"%PY%" -c "import fastapi, uvicorn, httpx, sqlalchemy, cryptography, pydantic, websockets" >nul 2>nul
if errorlevel 1 (
  echo   [INFO] Dependencies missing, installing requirements-desktop.txt ...
  "%PY%" -m pip install -r requirements-desktop.txt
  if errorlevel 1 (
    echo   [ERROR] Install failed. Try manually:
    echo           "%PY%" -m pip install -r requirements-desktop.txt
    pause
    exit /b 1
  )
)

echo   Starting... (tray icon appears shortly; minimise this window if you like.
echo   Closing this window stops the service.)
echo.
"%PY%" -m airelay %*
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
  echo.
  echo   [exit %CODE%] Failed to start. Common causes:
  echo     - port already in use ^(try:  <this script> --port 8090^)
  echo     - data directory not writable
  echo   Run the built-in doctor for details:
  echo           "%PY%" -m airelay --doctor
  echo.
  pause
)
endlocal

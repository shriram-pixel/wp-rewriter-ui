@echo off
setlocal
title WordPress Agent

REM Always run from this script's own folder (the project folder)
cd /d "%~dp0"

echo.
echo ==================================================
echo   WordPress Agent - updating and launching
echo ==================================================
echo.

REM ---- pull the latest files from the repo ----
where git >nul 2>&1
if errorlevel 1 (
  echo [WARN] Git not found on PATH - skipping update, launching current files.
) else (
  echo Getting the latest updates from the repo...
  git pull --autostash
)

REM ---- choose Python: the project's own .venv if present, else system ----
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )
if not defined PY ( where py >nul 2>&1 && set "PY=py" )
if not defined PY (
  echo [ERROR] Python was not found. Please run install.bat first.
  echo.
  pause
  exit /b 1
)

REM ---- install any newly added dependencies (quick if nothing changed) ----
if exist "requirements.txt" (
  echo Checking dependencies...
  "%PY%" -m pip install -q -r requirements.txt
)

REM ---- open the browser a few seconds after the server starts ----
start "" powershell -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:5000/'"

echo.
echo Server starting at  http://127.0.0.1:5000/
echo Leave this window open while you use the app. Press Ctrl+C here to stop it.
echo.

REM ---- run the app (stays running until you stop it) ----
"%PY%" app.py

echo.
echo The server has stopped.
pause

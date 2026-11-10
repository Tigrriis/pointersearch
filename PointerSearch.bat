@echo off
rem Double-click to start PointerSearch. Sets itself up on first run.
setlocal
cd /d "%~dp0"
title PointerSearch
set "PY=python"
where py >nul 2>nul && set "PY=py"
if exist ".venv\Scripts\pointersearch.exe" goto run
echo First run: setting up PointerSearch. This needs internet and takes a few minutes.
%PY% -m venv .venv || goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e . || goto fail
:run
echo.
echo PointerSearch opens in your web browser.
echo Keep this window open while you use it. Close it to stop.
echo.
".venv\Scripts\pointersearch.exe" app %*
if errorlevel 1 goto fail
goto :eof
:fail
echo.
echo PointerSearch could not start. The messages above say why.
pause

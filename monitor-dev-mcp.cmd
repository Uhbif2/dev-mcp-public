@echo off

chcp 65001 >nul

setlocal

set "HERE=%~dp0"

set "PYTHONUTF8=1"

set "PYTHONIOENCODING=utf-8"

if exist "%HERE%.venv\Scripts\python.exe" (

  "%HERE%.venv\Scripts\python.exe" "%HERE%tools\monitor_audit.py"

) else (

  py -3 "%HERE%tools\monitor_audit.py"

)

if errorlevel 1 (

  echo.

  echo [ERROR] monitor exited with code %ERRORLEVEL%

  pause

)


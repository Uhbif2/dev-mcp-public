@echo off
REM Owner console for the dev-mcp fleet registry. ASCII source; output is UTF-8.
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%src"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%HERE%.venv\Scripts\python.exe" (
  echo [ERROR] Python venv not found: %HERE%.venv\Scripts\python.exe
  exit /b 2
)
"%HERE%.venv\Scripts\python.exe" "%HERE%tools\fleet.py" %*
exit /b %ERRORLEVEL%

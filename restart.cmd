@echo off
REM One button: validate policy.toml, then restart the dev-mcp service and verify
REM both the local hop and the public Oracle-gateway hop (what ChatGPT uses).
REM Double-click after changing dev-mcp code. (policy.toml alone is hot-reloaded -
REM the running server picks it up in seconds; check.cmd validates it.)
REM By default restarts ONLY the dev instance (:8766) - seconds, and memory_service /
REM obsidian on :8765 keep running untouched.
REM   restart.cmd --core         restart the core instance (:8765) instead
REM   restart.cmd --all          restart both instances
REM   restart.cmd --local-only   only restart + local check
REM   restart.cmd --no-tests     skip the production-guard tests
REM ASCII only on purpose: cmd.exe reads .cmd in the OEM codepage.
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%src"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
REM Same execution PATH as the worker started through run.cmd.
set "PATH=%PATH%;%USERPROFILE%\memory_service\.venv\Scripts;%USERPROFILE%\platform-tools"
"%HERE%.venv\Scripts\python.exe" "%HERE%tools\restart.py" %*
set "RESTART_EXIT=%ERRORLEVEL%"
echo.
if not "%RESTART_EXIT%"=="0" echo [ERROR] Restart failed with exit code %RESTART_EXIT%.
pause
exit /b %RESTART_EXIT%

@echo off
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%src"
set "PYTHONUTF8=1"
"%HERE%.venv\Scripts\python.exe" -m dev_mcp --check --policy "%HERE%policy.toml"
exit /b %ERRORLEVEL%

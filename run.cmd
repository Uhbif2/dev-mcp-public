@echo off
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%src"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%HERE%.venv\Scripts\python.exe" -m dev_mcp --policy "%HERE%policy.toml" --audit "%HERE%logs\audit.jsonl"

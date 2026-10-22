@echo off
REM Один раз подключить сервер dev-mcp к официальному MCP TickTick (OAuth). Повтор - переподключение.
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\ticktick_connect.py
pause

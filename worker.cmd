@echo off

setlocal

set "ROOT=%~dp0"

if exist "%ROOT%.venv\Scripts\python.exe" (

  "%ROOT%.venv\Scripts\python.exe" "%ROOT%tools\worker.py" %*

) else (

  py -3 "%ROOT%tools\worker.py" %*

)

exit /b %ERRORLEVEL%


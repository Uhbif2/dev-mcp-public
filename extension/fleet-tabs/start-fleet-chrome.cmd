@echo off
rem Отдельный Chrome для вкладок-агентов флота: свой профиль, своя память,
rem не мешает основному браузеру. %~dp0 всегда заканчивается "\": если подставить его
rem прямо перед закрывающей кавычкой, Windows склеивает ВСЕ следующие флаги в один argv.
set "FLEET_EXT=%~dp0"
set "FLEET_EXT=%FLEET_EXT:~0,-1%"
rem Без файла маршрутов сохраняется обычный маршрут Chrome. С файлом — только локальный
rem исход флота; никакого обхода мёртвого прокси через DIRECT.
set "FLEET_PROXY="
if exist "%~dp0..\..\data\secrets\fleet-egress.txt" set "FLEET_PROXY=--proxy-server=http://127.0.0.1:8899"
rem 17.11.2026 (владелец): флаги против троттлинга фоновых вкладок и держатель приоритета
rem (fleet-priority.ps1) выключены — скрытые вкладки ChatGPT рисовали ответы на полной скорости,
rem Chrome ел 65%% ЦП. Отправка выводит вкладку вперёд сама (fleet-tabs 0.19.33), ход агента
rem идёт на сервере, вкладка после отправки закрывается.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --user-data-dir="%LOCALAPPDATA%\FleetChrome" ^
  --remote-debugging-address=127.0.0.1 ^
  --remote-debugging-port=9223 ^
  %FLEET_PROXY% ^
  --start-minimized ^
  --new-window ^
  --no-first-run ^
  https://chatgpt.com/

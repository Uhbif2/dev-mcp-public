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
rem Флаги ниже не дают Chrome притормаживать фоновые вкладки.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --user-data-dir="%LOCALAPPDATA%\FleetChrome" ^
  --remote-debugging-address=127.0.0.1 ^
  --remote-debugging-port=9223 ^
  %FLEET_PROXY% ^
  --start-minimized ^
  --new-window ^
  --no-first-run ^
  --disable-background-timer-throttling ^
  --disable-renderer-backgrounding ^
  --disable-backgrounding-occluded-windows ^
  --disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion ^
  https://chatgpt.com/
rem 23.09.2026: «Экономия памяти» выключена, а вкладки всё равно «оптимизировались» — это
rem заморозка фоновых вкладок и сильное торможение таймеров, их отключают флаги выше. Что
rem именно делает Chrome с вкладкой — chrome://discards (столбец Lifecycle State).
rem Флаги выше в Chrome 154 не мешают ему ставить невидимым вкладкам приоритет Idle (22.09.2026:
rem 13 из 14 вкладок) — держатель приоритета возвращает им Normal, пока открыт этот Chrome.
wscript.exe "%~dp0fleet-priority-hidden.vbs"

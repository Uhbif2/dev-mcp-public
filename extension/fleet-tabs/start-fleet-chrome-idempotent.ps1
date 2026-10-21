$ErrorActionPreference = 'Stop'

$existing = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*FleetChrome*' }

if ($existing) {
    Write-Output ("fleet_chrome=already_running count={0}" -f @($existing).Count)
    exit 0
}

$cmd = Join-Path $PSScriptRoot 'start-fleet-chrome.cmd'
if (-not (Test-Path $cmd)) {
    throw "missing launcher: $cmd"
}

Start-Process -FilePath 'cmd.exe' -ArgumentList @('/d','/c',('"' + $cmd + '"')) -WorkingDirectory $PSScriptRoot
Start-Sleep -Seconds 3

$started = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*FleetChrome*' }

if (-not $started) {
    throw 'FleetChrome process did not appear after launcher start'
}

Write-Output ("fleet_chrome=started count={0}" -f @($started).Count)

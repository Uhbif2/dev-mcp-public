$ErrorActionPreference = 'Stop'

$chrome = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*FleetChrome*' }

foreach ($p in $chrome) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
}

$helpers = Get-CimInstance Win32_Process -Filter "Name='wscript.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*fleet-priority-hidden.vbs*' }
foreach ($p in $helpers) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
}

$deadline = [DateTime]::UtcNow.AddSeconds(10)
do {
    Start-Sleep -Milliseconds 300
    $left = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*FleetChrome*' }
} while ($left -and [DateTime]::UtcNow -lt $deadline)

if ($left) { throw "FleetChrome processes did not exit cleanly" }

$cmd = Join-Path $PSScriptRoot 'start-fleet-chrome.cmd'
Start-Process -FilePath 'cmd.exe' -ArgumentList @('/d','/c',('"' + $cmd + '"')) -WorkingDirectory $PSScriptRoot
Start-Sleep -Seconds 4

$started = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*FleetChrome*' }

if (-not $started) { throw 'FleetChrome did not restart' }
Write-Output ("fleet_chrome=restarted count={0}" -f @($started).Count)

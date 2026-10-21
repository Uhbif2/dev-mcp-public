# Держатель приоритета вкладок флотского Chrome.
#
# 22.09.2026: 13 из 14 процессов вкладок FleetChrome стояли на приоритете Idle — Chrome сам
# «опускает» невидимые вкладки (Idle + EcoQoS на Windows 11), и флаг
# --disable-renderer-backgrounding в Chrome 154 этого не отменяет. При загруженной машине (в
# пике 0.3-0.6 ГБ свободной памяти) такие вкладки получают процессор последними, поток ответа
# ChatGPT в них захлёбывается — отсюда, по всей видимости, «Соединение прервано. Ожидание
# полного ответа».
#
# Раз в IntervalSec поднимает процессы вкладок (--type=renderer) FleetChrome до Priority, снимает
# с них EcoQoS и возвращает обычный приоритет памяти. Chrome перевыставит своё при смене
# видимости вкладки — следующий проход поправит. Ничего постоянного в системе не меняет:
# остановился скрипт — Chrome со временем вернёт всё сам. Выходит, когда FleetChrome закрыт.
# Запускается из start-fleet-chrome.cmd скрыто (fleet-priority-hidden.vbs); второй экземпляр
# сразу выходит.

param(
    [ValidateSet('Normal', 'BelowNormal', 'AboveNormal')] [string]$Priority = 'Normal',
    [int]$IntervalSec = 20,
    [switch]$Once
)

$ErrorActionPreference = 'Continue'
$Log = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'logs\fleet-priority.log'

function Write-Log([string]$line) {
    try {
        if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 1MB)) { Move-Item -Force $Log "$Log.old" }
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $line" | Out-File -FilePath $Log -Append -Encoding utf8
    } catch { }
}

$mutex = New-Object System.Threading.Mutex($false, 'Local\FleetChromePriorityKeeper')
if (-not $Once -and -not $mutex.WaitOne(0)) { exit 0 }

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class FleetPriority {
    [StructLayout(LayoutKind.Sequential)]
    struct PowerThrottling { public uint Version; public uint ControlMask; public uint StateMask; }
    [StructLayout(LayoutKind.Sequential)]
    struct MemoryPriority { public uint Value; }
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll")] static extern uint GetPriorityClass(IntPtr h);
    [DllImport("kernel32.dll")] static extern bool SetPriorityClass(IntPtr h, uint cls);
    [DllImport("kernel32.dll")] static extern bool SetProcessInformation(IntPtr h, int cls, ref PowerThrottling info, int size);
    [DllImport("kernel32.dll")] static extern bool SetProcessInformation(IntPtr h, int cls, ref MemoryPriority info, int size);
    const uint SET_INFORMATION = 0x0200, QUERY_LIMITED = 0x1000;
    // 1 — класс поднят; 0 — уже был не ниже; -1 — процесс не открылся (закрылся по дороге)
    public static int Raise(int pid, uint target) {
        IntPtr h = OpenProcess(SET_INFORMATION | QUERY_LIMITED, false, pid);
        if (h == IntPtr.Zero) return -1;
        try {
            var off = new PowerThrottling { Version = 1, ControlMask = 1, StateMask = 0 }; // EcoQoS снят явно
            SetProcessInformation(h, 4, ref off, Marshal.SizeOf(typeof(PowerThrottling)));
            var mem = new MemoryPriority { Value = 5 }; // MEMORY_PRIORITY_NORMAL
            SetProcessInformation(h, 0, ref mem, Marshal.SizeOf(typeof(MemoryPriority)));
            uint current = GetPriorityClass(h);
            // IDLE 0x40, BELOW_NORMAL 0x4000 — ниже цели; NORMAL 0x20 и выше не трогаем
            bool low = current == 0x40 || (current == 0x4000 && target != 0x4000);
            if (low && SetPriorityClass(h, target)) return 1;
            return 0;
        } finally { CloseHandle(h); }
    }
}
'@

$target = @{ Normal = 0x20; BelowNormal = 0x4000; AboveNormal = 0x8000 }[$Priority]
$needle = 'Fleet' + 'Chrome'   # целиком строки в командной строке запроса нет — см. _split_literal в tools/restart.py
$missing = 0
Write-Log "старт: приоритет $Priority, раз в $IntervalSec с"

while ($true) {
    try {
        $all = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object { $_.CommandLine -like "*$needle*" }
        $browser = $all | Where-Object { $_.CommandLine -notlike '*--type=*' }
        if (-not $browser) {
            $missing += 1
            if ($Once) { Write-Log 'FleetChrome закрыт'; break }
            if ($missing -ge 3) {
                # Этот helper отвечает только за приоритет renderer-процессов. Раньше он сам
                # пытался воскресить Chrome через cmd/PowerShell и мог бесконечно писать
                # «запускаю заново», хотя browser не жил. Внешний supervisor dev-mcp теперь
                # владеет liveness/restart и проверяет настоящий DevTools readiness.
                Write-Log 'FleetChrome закрыт — выхожу; перезапуском владеет dev-mcp supervisor'
                break
            }
        } else {
            $missing = 0
            $raised = 0
            foreach ($p in ($all | Where-Object { $_.CommandLine -like '*--type=renderer*' })) {
                if ([FleetPriority]::Raise([int]$p.ProcessId, [uint32]$target) -eq 1) { $raised += 1 }
            }
            if ($raised -gt 0) { Write-Log "поднято процессов вкладок: $raised" }
        }
    } catch {
        Write-Log "ошибка прохода: $($_.Exception.Message)"
    }
    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSec
}

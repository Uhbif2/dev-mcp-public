param([Parameter(Mandatory=$true)][string]$Workspace, [string]$Project = 'Demo')
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
if (-not (Test-Path -LiteralPath $Workspace)) { New-Item -ItemType Directory -Path $Workspace | Out-Null }
if (Get-Command uv -ErrorAction SilentlyContinue) {
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
} else {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        & py -3 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required' }
    }
    & .venv\Scripts\python.exe -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
}
& .venv\Scripts\python.exe tools\standalone.py init --workspace $Workspace --project $Project
if ($LASTEXITCODE -ne 0) { throw 'Configuration already exists or is invalid; no overwrite performed' }
& .venv\Scripts\python.exe tools\standalone.py doctor --configuration-only
exit $LASTEXITCODE

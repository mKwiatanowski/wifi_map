#Requires -Version 5.1
<#
.SYNOPSIS
    Development run on the work laptop: simulated household only, data in .\data-dev.
    Never talks to a real router or AdGuard. No administrator rights needed.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $RepoRoot
try {
    $venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    $homesense = Join-Path $RepoRoot '.venv\Scripts\homesense.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $python = Get-Command python -ErrorAction SilentlyContinue
        $version = $null
        if ($python) { $version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" }
        if ($LASTEXITCODE -ne 0 -or -not $version -or [version]$version -lt [version]'3.12') {
            throw "HomeSense needs Python 3.12 or newer on PATH (found: $version). Install it, e.g. winget install Python.Python.3.12"
        }
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'python -m venv failed' }
    }
    & $venvPython -m pip install -e ".[dev,desktop]"
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

    $env:HOMESENSE_MODE = "simulated"
    $env:HOMESENSE_DATA_DIR = ".\data-dev"
    $isNewDb = -not (Test-Path -LiteralPath '.\data-dev\homesense.db')

    & $homesense init-db
    if ($LASTEXITCODE -ne 0) { throw 'homesense init-db failed' }
    if ($isNewDb) {
        & $homesense simulate --days 8
        if ($LASTEXITCODE -ne 0) { throw 'homesense simulate failed' }
    }
    # Loopback only: the dev laptop must not expose the panel to the office network.
    & $homesense serve --host 127.0.0.1
} finally {
    Pop-Location
}

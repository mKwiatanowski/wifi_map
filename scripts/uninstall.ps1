#Requires -RunAsAdministrator
#Requires -Version 5.1
<#
.SYNOPSIS
    Removes the HomeSense service, firewall rules and tray shortcut. Data is kept unless -RemoveData.

.PARAMETER InstallDir
    Installation directory used by install.ps1. Default C:\HomeSense.

.PARAMETER RemoveAdGuard
    Also uninstall the AdGuard Home service (AdGuardHome.exe -s uninstall).

.PARAMETER RemoveData
    Also delete the data directory (homesense.db and data\auth\ with auth.db and setup-token.txt),
    .env (in InstallDir), logs and the venv. Without it all of these are kept.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = 'C:\HomeSense',
    [switch]$RemoveAdGuard,
    [switch]$RemoveData
)

$ErrorActionPreference = 'Stop'
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$ServiceExe = Join-Path $InstallDir 'HomeSense.exe'

Write-Host '==> Usługa HomeSense' -ForegroundColor Cyan
if (Get-Service -Name 'HomeSense' -ErrorAction SilentlyContinue) {
    if (Test-Path -LiteralPath $ServiceExe) {
        & $ServiceExe stopwait | Out-Host
        & $ServiceExe uninstall | Out-Host
    } else {
        Stop-Service -Name 'HomeSense' -ErrorAction SilentlyContinue
        & sc.exe delete HomeSense | Out-Host
    }
} else {
    Write-Host 'Usługa nie jest zainstalowana.'
}

if ($RemoveAdGuard) {
    Write-Host '==> AdGuard Home' -ForegroundColor Cyan
    $adGuardExe = Join-Path $InstallDir 'AdGuardHome\AdGuardHome.exe'
    if (Test-Path -LiteralPath $adGuardExe) {
        & $adGuardExe -s uninstall | Out-Host
        Write-Host 'Pamiętaj: zmień w routerze Primary DNS z powrotem (inaczej sieć straci DNS).' -ForegroundColor Yellow
    } else {
        Write-Host "Nie znaleziono $adGuardExe."
    }
}

Write-Host '==> Reguły zapory i skrót autostartu' -ForegroundColor Cyan
Get-NetFirewallRule -Group 'HomeSense' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'HomeSense.lnk'
if (Test-Path -LiteralPath $shortcut) { Remove-Item -LiteralPath $shortcut -Force }

if ($RemoveData) {
    Write-Host '==> Dane, .env, logi i venv' -ForegroundColor Cyan
    foreach ($name in @('data', 'logs', 'venv', 'grafana-config', '.env', 'HomeSense.exe', 'HomeSense.xml')) {
        $path = Join-Path $InstallDir $name
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
    }
    [Environment]::SetEnvironmentVariable('HOMESENSE_DATA_DIR', $null, 'Machine')
} else {
    Write-Host "Dane zachowane w $InstallDir (data, w tym konta w data\auth, oraz .env); usuń je parametrem -RemoveData."
}
Write-Host 'Grafana nie jest odinstalowywana (winget uninstall GrafanaLabs.Grafana.OSS, jeśli nie jest potrzebna).'
Write-Host 'Gotowe.' -ForegroundColor Green

#Requires -RunAsAdministrator
#Requires -Version 5.1
<#
.SYNOPSIS
    Installs HomeSense on the target Windows 11 machine (running 24/7): Python venv, Windows service
    (WinSW), AdGuard Home (DNS), Grafana OSS with the SQLite datasource, firewall rules, tray shortcut.

.DESCRIPTION
    Safe to re-run: existing .env, data and users are kept; the service is re-registered.
    Order matters: AdGuard Home's first-run wizard listens on port 3000, which Grafana also uses, so
    AdGuard is installed and configured (admin port 3001) before Grafana is installed.

.PARAMETER InstallDir
    Target directory (venv, .env, data, logs, service wrapper). Default C:\HomeSense.

.PARAMETER SkipAdGuard
    Do not install AdGuard Home.

.PARAMETER SkipGrafana
    Do not install or configure Grafana.

.PARAMETER AdminUser
    Name of the first panel account created with `homesense create-user`.

.NOTES
    Downloads are pinned to exact versions and checked against SHA-256 values recorded below before
    use (WinSW, AdGuard Home, the Grafana zip fallback). winget verifies the hashes of its own
    packages (Python, Grafana) from its signed manifest.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = 'C:\HomeSense',
    [switch]$SkipAdGuard,
    [switch]$SkipGrafana,
    [string]$AdminUser = 'admin'
)

$ErrorActionPreference = 'Stop'

# Pinned downloads. SHA-256: WinSW computed from the GitHub release asset (no published digest);
# AdGuard Home from the release's checksums.txt; Grafana from dl.grafana.com's .sha256 file.
$WinSwUrl = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
$WinSwSha256 = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'
$AdGuardVersion = 'v0.107.79'
$AdGuardUrl = "https://github.com/AdguardTeam/AdGuardHome/releases/download/$AdGuardVersion/AdGuardHome_windows_amd64.zip"
$AdGuardSha256 = 'e42294dc26b2c0d8f4576a91f09aa5129ffdfb7b647026fac77f0abac9acffce'
$GrafanaVersion = '12.2.0'
$GrafanaZipUrl = "https://dl.grafana.com/oss/release/grafana-$GrafanaVersion.windows-amd64.zip"
$GrafanaZipSha256 = 'e5d1bde97dd6f0920398c5cc3f03eebe314dcb6f20f04b0a94dcdea744b76780'
# The HomeSense service runs as its virtual account (no admin rights; only ping/arp/HTTP and :8080).
$ServiceAccount = 'NT SERVICE\HomeSense'
$AdGuardWizardUrl = 'http://127.0.0.1:3000'
$AdGuardStatusUrl = 'http://127.0.0.1:3001/control/status'
$GrafanaHealthUrl = 'http://127.0.0.1:3000/api/health'
$WizardTimeoutMin = 15
$MinPython = [version]'3.12'

# Well-known SIDs: account names are localized on Polish Windows ("Administratorzy").
$SidSystem = 'S-1-5-18'
$SidAdministrators = 'S-1-5-32-544'
$SidLocalService = 'S-1-5-19'
$SidUsers = 'S-1-5-32-545'

# --- helpers (pure ones are unit-tested in tests/test_scripts.py) ---

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [switch]$Quiet
    )
    if ($Quiet) { & $FilePath @ArgumentList | Out-Null } else { & $FilePath @ArgumentList | Out-Host }
    if ($LASTEXITCODE -ne 0) {
        throw ("Polecenie '{0} {1}' zakończyło się kodem {2}." -f $FilePath, ($ArgumentList -join ' '), $LASTEXITCODE)
    }
}

function Expand-Template {
    param([Parameter(Mandatory = $true)][string]$Text, [Parameter(Mandatory = $true)][hashtable]$Values)
    $result = $Text
    foreach ($key in $Values.Keys) {
        # String.Replace: no regex, so '$' and '\' in values stay literal.
        $result = $result.Replace('{{' + $key + '}}', [string]$Values[$key])
    }
    return $result
}

function ConvertTo-FileUriPath {
    <# A Windows path as the path part of a file: URI: forward slashes; %, #, ? and spaces encoded. #>
    param([Parameter(Mandatory = $true)][string]$Path)
    $uri = $Path.Replace('\', '/')
    $uri = $uri.Replace('%', '%25')  # first, so the escapes below are not double-encoded
    return $uri.Replace('#', '%23').Replace('?', '%3F').Replace(' ', '%20')
}

function ConvertTo-YamlSingleQuoted([string]$Value) {
    return $Value.Replace("'", "''")
}

function ConvertTo-XmlText([string]$Value) {
    return [System.Security.SecurityElement]::Escape($Value)
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding $false))
}

function New-SecretKey {
    <# 48 random bytes, base64url without padding (64 characters). #>
    $bytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Get-DotEnvValue {
    param([Parameter(Mandatory = $true)][string]$EnvFile, [Parameter(Mandatory = $true)][string]$Name)
    $pattern = '^\s*' + [regex]::Escape($Name) + '\s*=(.*)$'
    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
        $m = [regex]::Match($line, $pattern)
        if ($m.Success) { return $m.Groups[1].Value.Trim().Trim('"').Trim("'") }
    }
    return $null
}

function Set-DotEnvValue {
    <# Replaces NAME=... in place (first occurrence) or appends it; writes UTF-8 without BOM. #>
    param(
        [Parameter(Mandatory = $true)][string]$EnvFile,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )
    $text = [System.IO.File]::ReadAllText($EnvFile)
    $newline = "`n"
    if ($text.Contains("`r`n")) { $newline = "`r`n" }
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($line in ($text -split "`r?`n")) { $lines.Add($line) }
    if ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq '') { $lines.RemoveAt($lines.Count - 1) }
    $pattern = '^\s*' + [regex]::Escape($Name) + '\s*='
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = "$Name=$Value"
            $found = $true
            break
        }
    }
    if (-not $found) { $lines.Add("$Name=$Value") }
    Write-Utf8NoBom $EnvFile (($lines -join $newline) + $newline)
}

function Initialize-SecretKey {
    <# Generates HOMESENSE_SECRET_KEY when it is empty or the placeholder. Never prints the key. #>
    param([Parameter(Mandatory = $true)][string]$EnvFile)
    $current = Get-DotEnvValue -EnvFile $EnvFile -Name 'HOMESENSE_SECRET_KEY'
    if ([string]::IsNullOrWhiteSpace($current) -or $current -eq 'change-me') {
        Set-DotEnvValue -EnvFile $EnvFile -Name 'HOMESENSE_SECRET_KEY' -Value (New-SecretKey)
        Write-Host 'Wygenerowano losowy klucz HOMESENSE_SECRET_KEY i zapisano go w .env.'
    }
}

function Get-PrivateAclArguments {
    <#
        icacls arguments: drop inherited ACEs; Full for SYSTEM, Administrators and (if given) $UserSid;
        RX for $ReadExecuteSids.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$UserSid,
        [switch]$Container,
        [string[]]$ReadExecuteSids = @()
    )
    $inherit = ''
    if ($Container) { $inherit = '(OI)(CI)' }
    $arguments = @($Path, '/inheritance:r', '/grant:r', "*${SidSystem}:${inherit}F", "*${SidAdministrators}:${inherit}F")
    if ($UserSid) { $arguments += "*${UserSid}:${inherit}F" }
    foreach ($sid in $ReadExecuteSids) { $arguments += "*${sid}:${inherit}RX" }
    return , $arguments
}

function Set-PrivateAcl {
    <#
        Owner-only ACL (re-applied on every run, also to an existing path). -NoInstallingUser: no
        grant for the installing user (re-runs are elevated, Administrators suffice) and any earlier
        explicit grant for it is removed.
    #>
    param([Parameter(Mandatory = $true)][string]$Path, [string[]]$ReadExecuteSids = @(), [switch]$NoInstallingUser)
    $userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $container = Test-Path -LiteralPath $Path -PathType Container
    $granted = $userSid
    if ($NoInstallingUser) { $granted = $null }
    $arguments = Get-PrivateAclArguments -Path $Path -UserSid $granted -Container:$container -ReadExecuteSids $ReadExecuteSids
    Invoke-Native -Quiet icacls.exe $arguments
    if ($NoInstallingUser) { Invoke-Native -Quiet icacls.exe @($Path, '/remove:g', "*$userSid") }
}

function Get-AuthDirAclArguments {
    <#
        icacls arguments for <data>\auth: protected (no inheritance from the data dir, so none of
        Grafana's LocalService Modify), Full only for SYSTEM, Administrators (the elevated installer /
        create-user) and - once the service exists - its virtual account, plus an inheritable deny
        for LocalService as defence in depth. No grant for the installing user or Users.
    #>
    param([Parameter(Mandatory = $true)][string]$Path, [string]$ServiceAccount)
    $arguments = @(
        $Path, '/inheritance:r', '/grant:r',
        "*${SidSystem}:(OI)(CI)F", "*${SidAdministrators}:(OI)(CI)F"
    )
    if ($ServiceAccount) { $arguments += "${ServiceAccount}:(OI)(CI)F" }
    $arguments += @('/deny', "*${SidLocalService}:(OI)(CI)F")
    return , $arguments
}

function Set-AuthDirAcl {
    <#
        <data>\auth holds auth.db (+ -wal/-shm created later by the service) and setup-token.txt.
        Re-applied on every run. Files moved in from the old layout keep their old ACL, so the
        children are reset to inherit from the folder afterwards.
    #>
    New-Item -ItemType Directory -Force -Path $AuthDir | Out-Null
    $account = $null
    if (Get-Service -Name 'HomeSense' -ErrorAction SilentlyContinue) { $account = $ServiceAccount }
    Invoke-Native -Quiet icacls.exe (Get-AuthDirAclArguments -Path $AuthDir -ServiceAccount $account)
    if (Get-ChildItem -LiteralPath $AuthDir -Force) {
        Invoke-Native -Quiet icacls.exe @((Join-Path $AuthDir '*'), '/reset', '/T')
    }
}

function Save-Download([string]$Url, [string]$OutFile) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $previous = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'  # the progress bar makes Invoke-WebRequest very slow in 5.1
    try {
        Write-Host "Pobieranie $Url"
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    } finally {
        $ProgressPreference = $previous
    }
}

function Assert-FileHash {
    <# Throws (and deletes the file) unless its SHA-256 equals $Sha256. #>
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Sha256)
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($actual -ne $Sha256.ToUpperInvariant()) {
        Remove-Item -LiteralPath $Path -Force
        throw ("Niezgodna suma SHA-256 pliku {0}: oczekiwano {1}, jest {2}. Plik usunięto." -f $Path, $Sha256, $actual)
    }
}

function Save-VerifiedDownload([string]$Url, [string]$OutFile, [string]$Sha256) {
    <# Downloads next to $OutFile and moves it into place only after the SHA-256 check passed. #>
    $partial = "$OutFile.download"
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    Save-Download $Url $partial
    Assert-FileHash -Path $partial -Sha256 $Sha256
    Move-Item -LiteralPath $partial -Destination $OutFile -Force
}

function Get-ScConfigArgumentString {
    <#
        One verbatim argument string for `sc.exe config` (see Invoke-ScConfig): binPath= as a quoted
        absolute path (no unquoted service path) and obj= with an explicit empty password= "".
    #>
    param([Parameter(Mandatory = $true)][string]$Service, [string]$Account, [string]$BinaryPath)
    $parts = @('config', $Service)
    if ($BinaryPath) { $parts += ('binPath= "\"{0}\""' -f $BinaryPath) }
    if ($Account) { $parts += ('obj= "{0}" password= ""' -f $Account) }
    return ($parts -join ' ')
}

function Invoke-ScConfig {
    <#
        sc.exe needs `password= ""` as its own empty argument, which Windows PowerShell 5.1 drops when
        calling a native command; Start-Process with a single argument string passes it verbatim.
    #>
    param([Parameter(Mandatory = $true)][string]$Service, [string]$Account, [string]$BinaryPath)
    $argumentString = Get-ScConfigArgumentString -Service $Service -Account $Account -BinaryPath $BinaryPath
    $process = Start-Process -FilePath sc.exe -ArgumentList $argumentString -NoNewWindow -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "sc.exe $argumentString zakończył się kodem $($process.ExitCode)." }
}

function Test-QuotedServicePath([string]$PathName) {
    <# True when a service ImagePath cannot be misread: quoted, or an unquoted path without spaces. #>
    if ([string]::IsNullOrWhiteSpace($PathName)) { return $false }
    $trimmed = $PathName.Trim()
    if ($trimmed.StartsWith('"')) { return ($trimmed.IndexOf('"', 1) -gt 1) }
    $exeEnd = $trimmed.ToLowerInvariant().IndexOf('.exe')
    if ($exeEnd -lt 0) { return -not $trimmed.Contains(' ') }
    return -not $trimmed.Substring(0, $exeEnd).Contains(' ')
}

function Test-ServiceReadablePython([string]$Path) {
    <# The service account cannot read a per-user Python under the installing user's profile. #>
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    $full = [System.IO.Path]::GetFullPath($Path)
    foreach ($root in @($env:USERPROFILE, $env:LOCALAPPDATA, $env:APPDATA)) {
        if ($root -and $full.StartsWith($root.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
    }
    return $true
}

function Test-HttpEndpoint([string]$Url) {
    <# True when anything answers over HTTP (401/403 included: AdGuard wants credentials). #>
    try {
        $null = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return $true
    } catch [System.Net.WebException] {
        return ($null -ne $_.Exception.Response)
    } catch {
        return $false
    }
}

function Wait-HttpEndpoint([string]$Url, [int]$TimeoutMin) {
    $deadline = (Get-Date).AddMinutes($TimeoutMin)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpEndpoint $Url) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Update-ProcessPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Find-Python {
    <# Absolute path of a machine-wide Python >= 3.12 (readable by the service account), or $null. #>
    $probe = "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2])"
    $candidates = @(
        @('py', '-3.13'), @('py', '-3.12'), @('py', '-3'), @('python'),
        @("$env:ProgramFiles\Python313\python.exe"), @("$env:ProgramFiles\Python312\python.exe")
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'  # a missing launcher / Store alias must not abort the probe
    try {
        foreach ($candidate in $candidates) {
            $parts = @($candidate)
            $exe = $parts[0]
            if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
            $prefix = @()
            if ($parts.Count -gt 1) { $prefix = $parts[1..($parts.Count - 1)] }
            try {
                $out = & $exe @prefix -c $probe 2>$null
            } catch {
                continue
            }
            if ($LASTEXITCODE -ne 0 -or -not $out -or @($out).Count -lt 2) { continue }
            $lines = @($out)
            if ([version]$lines[1] -lt $MinPython) { continue }
            if (-not (Test-ServiceReadablePython $lines[0])) {
                Write-Host "Pomijam $($lines[0]): instalacja w profilu użytkownika jest niedostępna dla konta usługi."
                continue
            }
            return $lines[0]
        }
    } finally {
        $ErrorActionPreference = $previous
    }
    return $null
}

function Find-GrafanaHome {
    <# Grafana's home directory (contains conf\defaults.ini), from the usual locations or the service. #>
    $candidates = New-Object System.Collections.Generic.List[string]
    $candidates.Add((Join-Path $env:ProgramFiles 'GrafanaLabs\grafana'))
    $candidates.Add((Join-Path $InstallDir 'grafana-oss'))
    $service = Get-CimInstance Win32_Service -Filter "Name='Grafana'" -ErrorAction SilentlyContinue
    if ($service -and $service.PathName) {
        $m = [regex]::Match($service.PathName, '^"([^"]+)"|^(\S+)')
        $exe = $m.Groups[1].Value
        if (-not $exe) { $exe = $m.Groups[2].Value }
        $dir = Split-Path -Parent $exe
        for ($i = 0; $i -lt 3 -and $dir; $i++) {
            $candidates.Add($dir)
            $dir = Split-Path -Parent $dir
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate 'conf\defaults.ini')) { return $candidate }
    }
    return $null
}

function Get-LanAddress {
    $address = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -in @('Dhcp', 'Manual') } |
        Select-Object -First 1
    if ($address) { return $address.IPAddress }
    return '<IP-komputera>'
}

# --- installation steps ---

function Install-PythonVenv {
    Write-Step 'Python i środowisko wirtualne'
    $python = Find-Python
    if (-not $python) {
        Write-Host "Nie znaleziono Pythona >= $MinPython - instaluję przez winget."
        Invoke-Native winget @('install', '--id', 'Python.Python.3.12', '-e', '--scope', 'machine', '--silent',
            '--accept-package-agreements', '--accept-source-agreements')
        Update-ProcessPath
        $python = Find-Python
        if (-not $python) { throw 'Python 3.12 nadal niedostępny. Uruchom ponownie PowerShell (nowy PATH) i powtórz instalację.' }
    }
    Write-Host "Python: $python"
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Invoke-Native $python @('-m', 'venv', $VenvDir)
    }
    Invoke-Native $VenvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
    Invoke-Native $VenvPython @('-m', 'pip', 'install', '--upgrade', ('{0}[desktop,windows]' -f $RepoRoot))
}

function Initialize-Configuration {
    Write-Step 'Katalog danych i plik .env'
    foreach ($dir in @($DataDir, $LogDir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    # The data dir holds homesense.db and auth\ (auth.db, setup-token.txt); logs may contain file paths.
    Set-PrivateAcl $DataDir
    Set-PrivateAcl $LogDir
    Set-AuthDirAcl

    $created = $false
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Copy-Item -LiteralPath (Join-Path $RepoRoot '.env.example') -Destination $EnvFile
        Set-DotEnvValue -EnvFile $EnvFile -Name 'HOMESENSE_DATA_DIR' -Value $DataDir
        # A service account has no desktop session: toasts would never show (tray/browser/Telegram do).
        Set-DotEnvValue -EnvFile $EnvFile -Name 'HOMESENSE_WINDOWS_TOAST' -Value 'false'
        $created = $true
    }
    Set-PrivateAcl $EnvFile
    if ($created -or [string]::IsNullOrWhiteSpace((Get-DotEnvValue -EnvFile $EnvFile -Name 'HOMESENSE_ROUTER_PASSWORD'))) {
        Write-Host 'Otwieram .env w Notatniku: wpisz HOMESENSE_ROUTER_PASSWORD (hasło do panelu routera),'
        Write-Host 'opcjonalnie dane AdGuard Home (HOMESENSE_ADGUARD_USERNAME/PASSWORD) i powiadomień. Zapisz i zamknij Notatnik.'
        Start-Process -FilePath notepad.exe -ArgumentList ('"{0}"' -f $EnvFile) -Wait
    }
    Initialize-SecretKey -EnvFile $EnvFile

    [Environment]::SetEnvironmentVariable('HOMESENSE_DATA_DIR', $DataDir, 'Machine')
    $env:HOMESENSE_DATA_DIR = $DataDir
}

function Initialize-Database {
    Write-Step 'Baza danych i konto administratora'
    Push-Location -LiteralPath $InstallDir  # .env is read from the working directory
    try {
        Invoke-Native $HomeSenseExe @('init-db')
        Set-AuthDirAcl  # init-db may have moved auth.db in from the old <data>\auth.db location
        $countCode = 'from homesense.core.config import Settings; from homesense.storage.auth import open_settings_auth_db; ' +
            'from homesense.storage.repositories import UserRepo; d = open_settings_auth_db(Settings()); ' +
            'print(UserRepo(d).count()); d.close()'
        $users = [int]((& $VenvPython -c $countCode) | Select-Object -Last 1)
        if ($LASTEXITCODE -ne 0) { throw 'Nie udało się odczytać liczby kont z auth.db.' }
        if ($users -gt 0) {
            Write-Host "Konto do panelu już istnieje (liczba kont: $users) - pomijam create-user."
            return
        }
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            Write-Host "Ustaw hasło konta '$AdminUser' do panelu HomeSense (zalecane co najmniej 8 znaków):"
            & $HomeSenseExe create-user $AdminUser
            if ($LASTEXITCODE -eq 0) { return }
        }
        Write-Warning 'Nie utworzono konta. Usługa zapisze jednorazowy token w data\auth\setup-token.txt - zobacz README (Pierwsze uruchomienie).'
    } finally {
        Pop-Location
    }
}

function Install-HomeSenseService {
    Write-Step 'Usługa Windows HomeSense (WinSW)'
    if (Get-Service -Name 'HomeSense' -ErrorAction SilentlyContinue) {
        Write-Host 'Usługa już istnieje - rejestruję ją ponownie.'
        if (Test-Path -LiteralPath $ServiceExe) {
            Invoke-Native $ServiceExe @('stopwait')
            Invoke-Native $ServiceExe @('uninstall')
        } else {
            Stop-Service -Name 'HomeSense' -ErrorAction SilentlyContinue
            Invoke-Native sc.exe @('delete', 'HomeSense')
        }
        for ($i = 0; $i -lt 30 -and (Get-Service -Name 'HomeSense' -ErrorAction SilentlyContinue); $i++) {
            Start-Sleep -Seconds 1
        }
    }
    Save-VerifiedDownload $WinSwUrl $ServiceExe $WinSwSha256
    $template = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'winsw\HomeSense.xml.template'))
    $xml = Expand-Template -Text $template -Values @{
        EXECUTABLE  = (ConvertTo-XmlText $HomeSenseExe)
        INSTALL_DIR = (ConvertTo-XmlText $InstallDir)
        DATA_DIR    = (ConvertTo-XmlText $DataDir)
        LOG_DIR     = (ConvertTo-XmlText $LogDir)
    }
    Write-Utf8NoBom (Join-Path $InstallDir 'HomeSense.xml') $xml
    Invoke-Native $ServiceExe @('install')
    # WinSW already registers the quoted path and <serviceaccount>; re-assert both explicitly.
    Invoke-ScConfig -Service 'HomeSense' -Account $ServiceAccount -BinaryPath $ServiceExe
    Assert-ServiceConfig -Name 'HomeSense' -Account $ServiceAccount
    Grant-ServiceAccess
    Invoke-Native $ServiceExe @('start')
}

function Assert-ServiceConfig([string]$Name, [string]$Account) {
    $service = Get-CimInstance Win32_Service -Filter "Name='$Name'"
    if (-not $service) { throw "Brak usługi $Name." }
    if ($Account -and $service.StartName -ne $Account) {
        throw "Usługa $Name działa jako '$($service.StartName)', oczekiwano '$Account'."
    }
    if (-not (Test-QuotedServicePath $service.PathName)) {
        throw "Usługa $Name ma niecytowaną ścieżkę: $($service.PathName)"
    }
}

function Grant-ServiceAccess {
    <#
        What NT SERVICE\HomeSense needs and nothing more: RX on the install dir (venv), Modify on the
        data dir and the WinSW log dir, Full on data\auth (with SYSTEM/Administrators), Read on .env.
    #>
    Invoke-Native -Quiet icacls.exe @($InstallDir, '/grant', "${ServiceAccount}:(OI)(CI)RX")
    Invoke-Native -Quiet icacls.exe @($DataDir, '/grant', "${ServiceAccount}:(OI)(CI)M")
    Invoke-Native -Quiet icacls.exe @($LogDir, '/grant', "${ServiceAccount}:(OI)(CI)M")
    Invoke-Native -Quiet icacls.exe @($EnvFile, '/grant', "${ServiceAccount}:R")
    Set-AuthDirAcl
}

function Install-AdGuardHome {
    <# Returns $true once AdGuard's admin interface answers on port 3001. #>
    Write-Step 'AdGuard Home (DNS)'
    $adGuardDir = Join-Path $InstallDir 'AdGuardHome'
    $adGuardExe = Join-Path $adGuardDir 'AdGuardHome.exe'
    if (-not (Test-Path -LiteralPath $adGuardExe)) {
        $zip = Join-Path $env:TEMP 'AdGuardHome_windows_amd64.zip'
        Save-VerifiedDownload $AdGuardUrl $zip $AdGuardSha256
        Expand-Archive -LiteralPath $zip -DestinationPath $InstallDir -Force  # the zip holds AdGuardHome\
        Remove-Item -LiteralPath $zip -Force
        if (-not (Test-Path -LiteralPath $adGuardExe)) { throw "Nie znaleziono $adGuardExe po rozpakowaniu archiwum." }
    }
    # AdGuardHome.yaml holds the admin password hash: no read access for ordinary users.
    Set-PrivateAcl $adGuardDir -NoInstallingUser  # AdGuard runs as LocalSystem: SYSTEM + Administrators only
    if (-not (Get-Service -Name 'AdGuardHome' -ErrorAction SilentlyContinue)) {
        Push-Location -LiteralPath $adGuardDir
        try { Invoke-Native $adGuardExe @('-s', 'install') } finally { Pop-Location }
    }
    if (Test-HttpEndpoint $AdGuardStatusUrl) {
        Write-Host 'AdGuard Home jest już skonfigurowany (port 3001 odpowiada).'
        return $true
    }
    Write-Host ''
    Write-Host "Otwieram kreator AdGuard Home: $AdGuardWizardUrl" -ForegroundColor Yellow
    Write-Host '  * Interfejs administratora: 127.0.0.1, port 3001'
    Write-Host '  * Serwer DNS: Wszystkie interfejsy, port 53'
    Write-Host '  * Ustaw login i hasło administratora (te same wpisz potem w .env: HOMESENSE_ADGUARD_USERNAME/PASSWORD)'
    Write-Host "Skrypt czeka, aż $AdGuardStatusUrl zacznie odpowiadać (maks. $WizardTimeoutMin min)..."
    Start-Process $AdGuardWizardUrl
    if (Wait-HttpEndpoint $AdGuardStatusUrl $WizardTimeoutMin) {
        Write-Host 'AdGuard Home odpowiada na porcie 3001.'
        return $true
    }
    Write-Warning "AdGuard Home nie odpowiada na porcie 3001 po $WizardTimeoutMin min. Grafana NIE zostanie zainstalowana (konflikt portu 3000)."
    Write-Warning 'Dokończ kreator i uruchom skrypt ponownie z parametrem -SkipAdGuard.'
    return $false
}

function Install-GrafanaFromZip {
    $grafanaDir = Join-Path $InstallDir 'grafana-oss'
    $zip = Join-Path $env:TEMP "grafana-$GrafanaVersion.windows-amd64.zip"
    $extract = Join-Path $env:TEMP "grafana-$GrafanaVersion-extract"
    Save-VerifiedDownload $GrafanaZipUrl $zip $GrafanaZipSha256
    if (Test-Path -LiteralPath $extract) { Remove-Item -LiteralPath $extract -Recurse -Force }
    Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force
    $defaults = Get-ChildItem -LiteralPath $extract -Recurse -Filter 'defaults.ini' | Select-Object -First 1
    if (-not $defaults) { throw 'Archiwum Grafany nie zawiera conf\defaults.ini.' }
    if (Test-Path -LiteralPath $grafanaDir) { Remove-Item -LiteralPath $grafanaDir -Recurse -Force }
    Move-Item -LiteralPath (Split-Path -Parent $defaults.Directory.FullName) -Destination $grafanaDir
    Remove-Item -LiteralPath $extract -Recurse -Force
    Remove-Item -LiteralPath $zip -Force

    # Register the "Grafana" service with a second WinSW copy, directly as LocalService.
    $server = Join-Path $grafanaDir 'bin\grafana-server.exe'
    $serverArgs = '--homepath "{0}"' -f $grafanaDir
    if (-not (Test-Path -LiteralPath $server)) {
        $server = Join-Path $grafanaDir 'bin\grafana.exe'
        $serverArgs = 'server --homepath "{0}"' -f $grafanaDir
    }
    $wrapper = Join-Path $grafanaDir 'GrafanaService.exe'
    Copy-Item -LiteralPath $ServiceExe -Destination $wrapper -Force
    $xml = @"
<service>
  <id>Grafana</id>
  <name>Grafana</name>
  <description>Grafana OSS for HomeSense (127.0.0.1:3000)</description>
  <executable>"$(ConvertTo-XmlText $server)"</executable>
  <arguments>$(ConvertTo-XmlText $serverArgs)</arguments>
  <workingdirectory>$(ConvertTo-XmlText $grafanaDir)</workingdirectory>
  <serviceaccount><domain>NT AUTHORITY</domain><user>LocalService</user></serviceaccount>
  <onfailure action="restart" delay="10 sec"/>
  <logpath>$(ConvertTo-XmlText (Join-Path $grafanaDir 'data\log'))</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>4</keepFiles></log>
</service>
"@
    New-Item -ItemType Directory -Force -Path (Join-Path $grafanaDir 'data\log') | Out-Null
    Invoke-Native -Quiet icacls.exe @($grafanaDir, '/grant', "*${SidLocalService}:(OI)(CI)RX")
    Write-Utf8NoBom (Join-Path $grafanaDir 'GrafanaService.xml') $xml
    Invoke-Native $wrapper @('install')
    return $grafanaDir
}

function Grant-GrafanaAccess([string]$GrafanaHome) {
    <#
        LocalService: Modify on Grafana's data and the HomeSense data dir (homesense.db's SQLite WAL
        needs write access to the folder even for read-only opens); denied on <data>\auth\.
    #>
    $grafanaData = Join-Path $GrafanaHome 'data'
    New-Item -ItemType Directory -Force -Path $grafanaData | Out-Null
    Invoke-Native -Quiet icacls.exe @($grafanaData, '/grant', "*${SidLocalService}:(OI)(CI)M")
    Invoke-Native -Quiet icacls.exe @($GrafanaConfDir, '/grant', "*${SidLocalService}:(OI)(CI)RX")
    Invoke-Native -Quiet icacls.exe @($DataDir, '/grant', "*${SidLocalService}:(OI)(CI)M")
    Set-AuthDirAcl
}

function Set-GrafanaServiceAccount {
    Invoke-ScConfig -Service 'Grafana' -Account 'NT AUTHORITY\LocalService'
    $service = Get-CimInstance Win32_Service -Filter "Name='Grafana'"
    if ($service -and -not (Test-QuotedServicePath $service.PathName)) {
        $exe = $service.PathName.Trim()
        if (Test-Path -LiteralPath $exe -PathType Leaf) {
            Invoke-ScConfig -Service 'Grafana' -BinaryPath $exe  # path without arguments: quote it
        } else {
            Write-Warning "Usługa Grafana ma niecytowaną ścieżkę z argumentami: $($service.PathName) - popraw ją ręcznie (sc.exe config Grafana binPath= ...)."
        }
    }
}

function Install-Grafana {
    Write-Step 'Grafana OSS'
    $grafanaHome = Find-GrafanaHome
    if (-not $grafanaHome -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        & winget install --id GrafanaLabs.Grafana.OSS -e --silent --accept-package-agreements --accept-source-agreements | Out-Host
        $grafanaHome = Find-GrafanaHome
    }
    if (-not $grafanaHome) {
        Write-Host 'winget niedostępny lub instalacja nieudana - instaluję Grafanę z archiwum zip.'
        $grafanaHome = Install-GrafanaFromZip | Select-Object -Last 1
    }
    Write-Host "Grafana: $grafanaHome"
    if (-not (Get-Service -Name 'Grafana' -ErrorAction SilentlyContinue)) { throw 'Brak usługi Windows "Grafana".' }
    Stop-Service -Name 'Grafana' -ErrorAction SilentlyContinue

    # Provisioning and dashboards, rendered with this machine's paths.
    $provisioningDir = Join-Path $GrafanaConfDir 'provisioning'
    $dashboardsDir = Join-Path $GrafanaConfDir 'dashboards'
    if (Test-Path -LiteralPath $GrafanaConfDir) { Remove-Item -LiteralPath $GrafanaConfDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $dashboardsDir | Out-Null
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'grafana\provisioning') -Destination $provisioningDir -Recurse
    Copy-Item -Path (Join-Path $RepoRoot 'grafana\dashboards\*.json') -Destination $dashboardsDir
    $yamlValues = @{
        DB_PATH        = (ConvertTo-YamlSingleQuoted (ConvertTo-FileUriPath (Join-Path $DataDir 'homesense.db')))
        DASHBOARDS_DIR = (ConvertTo-YamlSingleQuoted $dashboardsDir)
    }
    foreach ($file in Get-ChildItem -LiteralPath $provisioningDir -Recurse -Filter '*.yaml') {
        $text = [System.IO.File]::ReadAllText($file.FullName)
        Write-Utf8NoBom $file.FullName (Expand-Template -Text $text -Values $yamlValues)
    }
    $ini = [System.IO.File]::ReadAllText((Join-Path $RepoRoot 'grafana\grafana.ini.template'))
    $ini = Expand-Template -Text $ini -Values @{
        DATA_DIR         = $DataDir.Replace('\', '/')
        PROVISIONING_DIR = $provisioningDir.Replace('\', '/')
    }
    Write-Utf8NoBom (Join-Path $grafanaHome 'conf\custom.ini') $ini

    # SQLite datasource plugin into Grafana's default plugins dir (<home>\data\plugins).
    $pluginsDir = Join-Path $grafanaHome 'data\plugins'
    $cli = Join-Path $grafanaHome 'bin\grafana-cli.exe'
    $cliArgs = @()
    if (-not (Test-Path -LiteralPath $cli)) {
        $cli = Join-Path $grafanaHome 'bin\grafana.exe'
        $cliArgs = @('cli')
    }
    Invoke-Native $cli ($cliArgs + @('--homepath', $grafanaHome, '--pluginsDir', $pluginsDir, 'plugins', 'install', 'frser-sqlite-datasource'))

    # Defence in depth: Grafana runs as LocalService, which cannot read anything in <data>\auth\.
    Grant-GrafanaAccess $grafanaHome
    Set-GrafanaServiceAccount
    Start-Service -Name 'Grafana'
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline -and -not (Test-HttpEndpoint $GrafanaHealthUrl)) { Start-Sleep -Seconds 3 }
    if (Test-HttpEndpoint $GrafanaHealthUrl) {
        Write-Host 'Grafana działa na 127.0.0.1:3000 (konto NT AUTHORITY\LocalService).'
    } else {
        Write-Warning "Grafana nie odpowiada. Sprawdź $DataDir\grafana\logs i README: Rozwiązywanie problemów > Grafana nie startuje jako LocalService."
    }
}

function Set-FirewallRules {
    Write-Step 'Zapora Windows (tylko profil Prywatny)'
    Get-NetFirewallRule -Group 'HomeSense' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    # Grafana (3000) and AdGuard's admin interface (3001) get no rules: they stay local.
    New-NetFirewallRule -DisplayName 'HomeSense panel (TCP 8080)' -Group 'HomeSense' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8080 -Profile Private | Out-Null
    New-NetFirewallRule -DisplayName 'HomeSense DNS (UDP 53)' -Group 'HomeSense' -Direction Inbound -Action Allow -Protocol UDP -LocalPort 53 -Profile Private | Out-Null
    New-NetFirewallRule -DisplayName 'HomeSense DNS (TCP 53)' -Group 'HomeSense' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 53 -Profile Private | Out-Null
    $public = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.NetworkCategory -eq 'Public' }
    foreach ($netProfile in @($public)) {
        if ($netProfile) {
            Write-Warning ("Sieć '{0}' ma profil Publiczny - reguły nie zadziałają. Zmień: Set-NetConnectionProfile -InterfaceIndex {1} -NetworkCategory Private" -f $netProfile.Name, $netProfile.InterfaceIndex)
        }
    }
}

function New-TrayShortcut {
    Write-Step 'Skrót ikony w zasobniku (autostart)'
    $startup = [Environment]::GetFolderPath('Startup')
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path $startup 'HomeSense.lnk'))
    # pythonw.exe: no console window (homesense.exe is a console program).
    $shortcut.TargetPath = $VenvPythonw
    $shortcut.Arguments = '-m homesense.cli tray'
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.Description = 'HomeSense - ikona w zasobniku'
    $shortcut.Save()
}

function Write-Summary([bool]$AdGuardReady) {
    $ip = Get-LanAddress
    Write-Host ''
    Write-Host '=== HomeSense zainstalowany ===' -ForegroundColor Green
    Write-Host "Panel:        http://127.0.0.1:8080  (w sieci domowej: http://${ip}:8080)"
    Write-Host "Dane i .env:  $DataDir , $EnvFile"
    Write-Host "Logi usługi:  $LogDir"
    if (-not $AdGuardReady -and -not $SkipAdGuard) {
        Write-Host 'UWAGA: AdGuard Home nie został skonfigurowany - dokończ kreator i uruchom skrypt ponownie z -SkipAdGuard.' -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host 'Następne kroki:'
    Write-Host "  1. Router (TP-Link): Advanced > Network > DHCP Server > Primary DNS = $ip"
    Write-Host "  2. Router: Address Reservation - stały adres $ip dla komputera docelowego (MAC karty sieciowej)."
    Write-Host '  3. Telefony: w ustawieniach Wi-Fi tej sieci wybierz „stały prywatny adres” (iPhone: Prywatny adres Wi-Fi = Stały;'
    Write-Host '     Android: adres MAC urządzenia lub stały losowy), żeby urządzenie miało zawsze ten sam MAC.'
    Write-Host '  4. Po zmianie .env (np. dane AdGuard): Restart-Service HomeSense'
    Write-Host '  5. W panelu: Osoby - dodaj domowników, Urządzenia - przypisz telefony (Liczy się do obecności).'
}

# --- main ---

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $InstallDir 'venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPythonw = Join-Path $VenvDir 'Scripts\pythonw.exe'
$HomeSenseExe = Join-Path $VenvDir 'Scripts\homesense.exe'
$DataDir = Join-Path $InstallDir 'data'
$LogDir = Join-Path $InstallDir 'logs'
$EnvFile = Join-Path $InstallDir '.env'
$AuthDir = Join-Path $DataDir 'auth'
$ServiceExe = Join-Path $InstallDir 'HomeSense.exe'
$GrafanaConfDir = Join-Path $InstallDir 'grafana-config'

# Lock the install dir before anything is created in it: it would otherwise inherit C:\'s ACL
# (Authenticated Users: Modify), and the venv, HomeSense.exe/.xml (a LocalSystem service) and
# AdGuardHome\ live here. Users keep read/execute so the per-user tray shortcut can run the venv.
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Set-PrivateAcl $InstallDir -ReadExecuteSids @($SidUsers) -NoInstallingUser

Install-PythonVenv
Initialize-Configuration
Initialize-Database
Install-HomeSenseService

$adGuardReady = $false
if (-not $SkipAdGuard) {
    $adGuardReady = [bool](Install-AdGuardHome | Select-Object -Last 1)
}
Set-FirewallRules
New-TrayShortcut

if ($SkipGrafana) {
    Write-Host 'Pomijam Grafanę (-SkipGrafana).'
} elseif ($SkipAdGuard -or $adGuardReady) {
    try {
        Install-Grafana
    } catch {
        Write-Warning "Instalacja Grafany nie powiodła się: $($_.Exception.Message)"
        Write-Warning 'HomeSense działa bez wykresów. Usuń przyczynę i uruchom: .\scripts\install.ps1 -SkipAdGuard'
    }
}

Write-Summary $adGuardReady

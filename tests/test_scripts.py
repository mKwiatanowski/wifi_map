"""Windows install/dev scripts, the WinSW service template and the README.

The scripts are never executed here (they install services, firewall rules and downloads). PowerShell
is only used to parse them and, for a few pure helper functions, to run that single function's
definition extracted from the script's AST.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PS_SCRIPTS = ["install.ps1", "uninstall.ps1", "dev.ps1"]
WINSW_TEMPLATE = SCRIPTS / "winsw" / "HomeSense.xml.template"
README = ROOT / "README.md"
BOM = b"\xef\xbb\xbf"
PARSER = "[System.Management.Automation.Language.Parser]"
FUNCTION_AST = "[System.Management.Automation.Language.FunctionDefinitionAst]"

POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None, reason="Windows PowerShell 5.1 only on win32"
)


def _powershell(command: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env={**os.environ, **(env or {})},
    )


def _text(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8-sig")


# --- PowerShell syntax ---


@needs_powershell
@pytest.mark.parametrize("name", PS_SCRIPTS)
def test_powershell_script_parses(name):
    path = str(SCRIPTS / name).replace("'", "''")
    result = _powershell(
        "$errs = $null; $tokens = $null; "
        f"$null = {PARSER}::ParseFile('{path}', [ref]$tokens, [ref]$errs); "
        "if ($errs) { $errs | ForEach-Object { Write-Output $_.ToString() }; exit 1 }"
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("name", PS_SCRIPTS)
def test_non_ascii_scripts_have_utf8_bom(name):
    """Windows PowerShell 5.1 reads BOM-less scripts as ANSI, which would mangle Polish messages."""
    raw = (SCRIPTS / name).read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError:
        assert raw.startswith(BOM), f"{name} contains non-ASCII text but no UTF-8 BOM"


# --- helper functions of install.ps1, run in isolation ---


def _run_install_function(call: str, env: dict[str, str]) -> str:
    """Define install.ps1's functions (none of its top-level code runs) and evaluate `call`."""
    path = str(SCRIPTS / "install.ps1").replace("'", "''")
    command = (
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$errs = $null; $tokens = $null; "
        f"$ast = {PARSER}::ParseFile('{path}', [ref]$tokens, [ref]$errs); "
        f"$fns = $ast.FindAll({{ param($n) $n -is {FUNCTION_AST} }}, $false); "
        "foreach ($fn in $fns) { . ([scriptblock]::Create($fn.Extent.Text)) }; " + call
    )
    result = _powershell(command, env)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@needs_powershell
def test_db_path_is_rendered_as_file_uri_path(tmp_path):
    """{{DB_PATH}} is part of a file: URI: forward slashes, and %, #, ? and spaces percent-encoded."""
    folder = tmp_path / "Home Sense 50%#?x"
    raw = str(folder / "homesense.db")
    out = _run_install_function("ConvertTo-FileUriPath -Path $env:HS_TEST_PATH", {"HS_TEST_PATH": raw})
    expected = raw.replace("\\", "/").replace("%", "%25").replace("#", "%23").replace("?", "%3F")
    assert out == expected.replace(" ", "%20")
    assert "\\" not in out and " " not in out and "#" not in out and "?" not in out


@needs_powershell
def test_rendered_db_path_opens_read_only_with_sqlite(tmp_path):
    """SQLite itself (as Grafana's plugin does) must open the rendered URI on an awkward path."""
    folder = tmp_path / "Home Sense 50% #1"  # '?' is not a legal Windows file name character
    folder.mkdir()
    db_file = folder / "homesense.db"
    with sqlite3.connect(db_file) as c:
        c.execute("CREATE TABLE t(x)")
        c.execute("INSERT INTO t VALUES (1)")
    c.close()
    rendered = _run_install_function(
        "ConvertTo-FileUriPath -Path $env:HS_TEST_PATH", {"HS_TEST_PATH": str(db_file)}
    )
    conn = sqlite3.connect(f"file:{rendered}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (2)")
    finally:
        conn.close()


@needs_powershell
def test_expand_template_replaces_all_placeholders():
    out = _run_install_function(
        "Expand-Template -Text 'a={{A}};b={{B}};a2={{A}}' -Values @{ A = 'x$y'; B = 'C:\\p q' }", {}
    )
    assert out == "a=x$y;b=C:\\p q;a2=x$y"


@needs_powershell
def test_secret_key_is_48_random_bytes_url_safe():
    out = _run_install_function("New-SecretKey; New-SecretKey", {})
    first, second = out.splitlines()
    assert re.fullmatch(r"[A-Za-z0-9_-]{64}", first), first
    assert first != second


@needs_powershell
@pytest.mark.parametrize("current", ["HOMESENSE_SECRET_KEY=", "HOMESENSE_SECRET_KEY=change-me", None])
def test_missing_secret_key_is_generated_into_env(tmp_path, current):
    env_file = tmp_path / ".env"
    lines = ["# comment", "HOMESENSE_MODE=live"] + ([current] if current else []) + ["HOMESENSE_PORT=8080"]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = _run_install_function(
        "Initialize-SecretKey -EnvFile $env:HS_TEST_ENV", {"HS_TEST_ENV": str(env_file)}
    )
    raw = env_file.read_bytes()
    assert not raw.startswith(BOM)
    values = dict(
        line.split("=", 1) for line in raw.decode("utf-8").splitlines() if line and not line.startswith("#")
    )
    key = values["HOMESENSE_SECRET_KEY"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{64}", key)
    assert key not in out, "the generated key must never be printed"
    assert values["HOMESENSE_MODE"] == "live" and values["HOMESENSE_PORT"] == "8080"
    assert raw.decode("utf-8").count("HOMESENSE_SECRET_KEY=") == 1


@needs_powershell
def test_existing_secret_key_is_kept(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("HOMESENSE_SECRET_KEY=already-a-real-key-123\n", encoding="utf-8")
    _run_install_function("Initialize-SecretKey -EnvFile $env:HS_TEST_ENV", {"HS_TEST_ENV": str(env_file)})
    assert env_file.read_text(encoding="utf-8") == "HOMESENSE_SECRET_KEY=already-a-real-key-123\n"


@needs_powershell
def test_set_dotenv_value_replaces_or_appends(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("HOMESENSE_DATA_DIR=data\nHOMESENSE_PORT=8080\n", encoding="utf-8")
    _run_install_function(
        "Set-DotEnvValue -EnvFile $env:HS_TEST_ENV -Name HOMESENSE_DATA_DIR -Value 'C:\\Home Sense\\data'; "
        "Set-DotEnvValue -EnvFile $env:HS_TEST_ENV -Name HOMESENSE_NEW -Value 'v'",
        {"HS_TEST_ENV": str(env_file)},
    )
    assert env_file.read_text(encoding="utf-8").splitlines() == [
        "HOMESENSE_DATA_DIR=C:\\Home Sense\\data",
        "HOMESENSE_PORT=8080",
        "HOMESENSE_NEW=v",
    ]


# --- install.ps1 content rules ---


def test_install_requires_admin_and_documented_parameters():
    text = _text("install.ps1")
    assert re.search(r"^#Requires -RunAsAdministrator", text, re.MULTILINE | re.IGNORECASE)
    assert re.search(r"\$InstallDir\s*=\s*['\"]C:\\HomeSense['\"]", text)
    for switch in ("SkipAdGuard", "SkipGrafana"):
        assert re.search(rf"\[switch\]\s*\${switch}", text)


def test_install_uses_pinned_download_urls():
    text = _text("install.ps1")
    assert "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe" in text
    assert "$WinSwSha256 = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'" in text
    assert "$AdGuardVersion = 'v0.107.79'" in text
    assert (
        '"https://github.com/AdguardTeam/AdGuardHome/releases/download/$AdGuardVersion/'
        'AdGuardHome_windows_amd64.zip"' in text
    )
    assert "$AdGuardSha256 = 'e42294dc26b2c0d8f4576a91f09aa5129ffdfb7b647026fac77f0abac9acffce'" in text
    assert "$GrafanaVersion = '12.2.0'" in text
    assert "$GrafanaZipSha256 = 'e5d1bde97dd6f0920398c5cc3f03eebe314dcb6f20f04b0a94dcdea744b76780'" in text
    assert "static.adguard.com" not in text, "unversioned 'latest' URL"
    assert not re.search(r"\[string\]\$GrafanaVersion", text), "a version override would void the pinned hash"
    assert "GrafanaLabs.Grafana.OSS" in text  # winget verifies its own package hashes
    assert "frser-sqlite-datasource" in text


def test_every_download_is_hash_verified():
    text = _text("install.ps1")
    calls = [m.group(0) for m in re.finditer(r"^\s*Save-(?:Verified)?Download .*$", text, re.MULTILINE)]
    assert calls, "no downloads"
    plain = [c for c in calls if c.strip().startswith("Save-Download ")]
    assert plain == ["    Save-Download $Url $partial"], plain  # only inside Save-VerifiedDownload
    verified = [c.strip() for c in calls if c.strip().startswith("Save-VerifiedDownload ")]
    assert verified == [
        "Save-VerifiedDownload $WinSwUrl $ServiceExe $WinSwSha256",
        "Save-VerifiedDownload $AdGuardUrl $zip $AdGuardSha256",
        "Save-VerifiedDownload $GrafanaZipUrl $zip $GrafanaZipSha256",
    ]
    body = _function_body(text, "Save-VerifiedDownload")
    assert body.index("Assert-FileHash") < body.index("Move-Item")


@needs_powershell
def test_assert_file_hash_accepts_matching_file(tmp_path):
    import hashlib

    good = tmp_path / "ok.bin"
    good.write_bytes(b"homesense")
    digest = hashlib.sha256(b"homesense").hexdigest()  # lower case, as recorded in install.ps1
    _run_install_function(
        "Assert-FileHash -Path $env:HS_FILE -Sha256 $env:HS_SHA", {"HS_FILE": str(good), "HS_SHA": digest}
    )
    assert good.exists()


@needs_powershell
def test_assert_file_hash_rejects_and_deletes_mismatching_file(tmp_path):
    import hashlib

    bad = tmp_path / "tampered.bin"
    bad.write_bytes(b"tampered")
    path = str(SCRIPTS / "install.ps1").replace("'", "''")
    result = _powershell(
        "$ErrorActionPreference = 'Stop'; "
        f"$ast = {PARSER}::ParseFile('{path}', [ref]$null, [ref]$null); "
        f"foreach ($fn in $ast.FindAll({{ param($n) $n -is {FUNCTION_AST} }}, $false)) "
        "{ . ([scriptblock]::Create($fn.Extent.Text)) }; "
        "Assert-FileHash -Path $env:HS_FILE -Sha256 $env:HS_SHA",
        {"HS_FILE": str(bad), "HS_SHA": hashlib.sha256(b"original").hexdigest()},
    )
    assert result.returncode != 0
    assert not bad.exists(), "a file failing verification must not stay on disk"


def test_scripts_never_prompt_with_read_host():
    for name in PS_SCRIPTS:
        assert "read-host" not in _text(name).lower(), name


def test_adguard_wizard_wait_polls_admin_port_3001_for_15_minutes():
    text = _text("install.ps1")
    assert "http://127.0.0.1:3000" in text
    assert "http://127.0.0.1:3001/control/status" in text
    assert re.search(r"AddMinutes\(\s*15\s*\)|\$WizardTimeoutMin\s*=\s*15", text)


def test_firewall_rules_are_private_only_for_8080_and_53():
    text = _text("install.ps1")
    rules = [
        line
        for line in text.splitlines()
        if "New-NetFirewallRule" in line and not line.strip().startswith("#")
    ]
    assert rules, "no firewall rules"
    for line in rules:
        assert "-Profile Private" in line, line
    ports = {(m.group(1), m.group(2)) for m in re.finditer(r"-Protocol (TCP|UDP) -LocalPort (\d+)", text)}
    assert ports == {("TCP", "8080"), ("UDP", "53"), ("TCP", "53")}
    assert not re.search(r"LocalPort\s+300[01]", text), "Grafana/AdGuard admin ports stay local"


def test_grafana_hardening():
    text = _text("install.ps1")
    assert "GF_PLUGIN_UNSAFE" not in text.upper()
    assert "Invoke-ScConfig -Service 'Grafana' -Account 'NT AUTHORITY\\LocalService'" in text
    # Passed as one verbatim argument string: PS 5.1 would drop the empty `""` native argument.
    body = _function_body(text, "Invoke-ScConfig")
    assert "Start-Process -FilePath sc.exe -ArgumentList $argumentString" in body
    assert "S-1-5-19" in text  # LocalService, by SID (account names are localized)


def test_auth_dir_denies_local_service_for_files_created_later():
    text = _text("install.ps1")
    assert re.search(r"\$AuthDir = Join-Path \$DataDir 'auth'", text)
    # Inheritable (OI)(CI) deny on the folder: auth.db-wal/-shm and setup-token.txt created later
    # by the service inherit it; files moved in from the old layout are reset to inherit.
    assert (
        "Invoke-Native -Quiet icacls.exe (Get-AuthDirAclArguments -Path $AuthDir -ServiceAccount $account)"
        in text
    )
    body = _function_body(text, "Set-AuthDirAcl")
    assert body.index("Get-AuthDirAclArguments") < body.index("'/reset', '/T'")
    body = _function_body(text, "Initialize-Configuration")
    assert body.index("Set-PrivateAcl $DataDir") < body.index("Set-AuthDirAcl")
    assert "Set-AuthDirAcl" in _function_body(text, "Initialize-Database")
    assert "Set-AuthDirAcl" in _function_body(text, "Grant-GrafanaAccess")
    assert "auth.db-wal" not in _function_body(text, "Grant-GrafanaAccess")  # no per-file patching


@needs_powershell
def test_auth_dir_acl_is_protected_system_and_admins_only():
    out = _run_install_function(
        "$SidSystem = 'S-1-5-18'; $SidAdministrators = 'S-1-5-32-544'; $SidLocalService = 'S-1-5-19'; "
        "(Get-AuthDirAclArguments -Path 'C:\\HomeSense\\data\\auth') -join '|'",
        {},
    )
    args = out.split("|")
    assert args == [
        "C:\\HomeSense\\data\\auth",
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F",
        "/deny",
        "*S-1-5-19:(OI)(CI)F",
    ]
    granted = args[args.index("/grant:r") + 1 : args.index("/deny")]
    assert not any(a.startswith(("*S-1-5-19:", "*S-1-5-32-545:", "*S-1-5-21-")) for a in granted)


@needs_powershell
def test_auth_dir_acl_grants_service_account_once_it_exists():
    out = _run_install_function(
        "$SidSystem = 'S-1-5-18'; $SidAdministrators = 'S-1-5-32-544'; $SidLocalService = 'S-1-5-19'; "
        "(Get-AuthDirAclArguments -Path 'C:\\HomeSense\\data\\auth' -ServiceAccount 'NT SERVICE\\HomeSense')"
        " -join '|'",
        {},
    )
    args = out.split("|")
    granted = args[args.index("/grant:r") + 1 : args.index("/deny")]
    assert granted == ["*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "NT SERVICE\\HomeSense:(OI)(CI)F"]
    assert args[-2:] == ["/deny", "*S-1-5-19:(OI)(CI)F"]


@needs_powershell
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            "-Service HomeSense -Account 'NT SERVICE\\HomeSense' -BinaryPath 'C:\\Home Sense\\HomeSense.exe'",
            'config HomeSense binPath= "\\"C:\\Home Sense\\HomeSense.exe\\"" '
            'obj= "NT SERVICE\\HomeSense" password= ""',
        ),
        (
            "-Service Grafana -Account 'NT AUTHORITY\\LocalService'",
            'config Grafana obj= "NT AUTHORITY\\LocalService" password= ""',
        ),
    ],
)
def test_sc_config_argument_string_quotes_paths_and_empty_password(kwargs, expected):
    assert _run_install_function(f"Get-ScConfigArgumentString {kwargs}", {}) == expected


@needs_powershell
@pytest.mark.parametrize(
    ("path_name", "ok"),
    [
        ('"C:\\Home Sense\\HomeSense.exe"', True),
        ('"C:\\Program Files\\GrafanaLabs\\grafana\\svc\\nssm.exe" run', True),
        ("C:\\HomeSense\\HomeSense.exe", True),
        ("C:\\Program Files\\GrafanaLabs\\grafana\\bin\\grafana.exe server", False),
        ("C:\\Home Sense\\HomeSense.exe", False),
        ("", False),
    ],
)
def test_quoted_service_path_check(path_name, ok):
    out = _run_install_function("Test-QuotedServicePath $env:HS_PATH", {"HS_PATH": path_name})
    assert out == str(ok)


@needs_powershell
def test_per_user_python_is_not_used_for_the_service(tmp_path):
    profile = tmp_path / "Users" / "anna"
    env = {"USERPROFILE": str(profile), "LOCALAPPDATA": str(profile / "AppData" / "Local")}
    user_python = str(profile / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe")
    assert (
        _run_install_function("Test-ServiceReadablePython $env:HS_PY", {**env, "HS_PY": user_python})
        == "False"
    )
    machine = "C:\\Program Files\\Python312\\python.exe"
    assert _run_install_function("Test-ServiceReadablePython $env:HS_PY", {**env, "HS_PY": machine}) == "True"
    assert "Test-ServiceReadablePython $lines[0]" in _function_body(_text("install.ps1"), "Find-Python")


def test_service_runs_as_virtual_account_with_least_privilege():
    text = _text("install.ps1")
    assert "$ServiceAccount = 'NT SERVICE\\HomeSense'" in text
    install = _function_body(text, "Install-HomeSenseService")
    order = [
        "Invoke-Native $ServiceExe @('install')",
        "Invoke-ScConfig -Service 'HomeSense' -Account $ServiceAccount -BinaryPath $ServiceExe",
        "Assert-ServiceConfig -Name 'HomeSense' -Account $ServiceAccount",
        "Grant-ServiceAccess",
        "Invoke-Native $ServiceExe @('start')",
    ]
    positions = [install.index(step) for step in order]
    assert positions == sorted(positions)
    grants = _function_body(text, "Grant-ServiceAccess")
    for line in (
        "@($InstallDir, '/grant', \"${ServiceAccount}:(OI)(CI)RX\")",
        "@($DataDir, '/grant', \"${ServiceAccount}:(OI)(CI)M\")",
        "@($LogDir, '/grant', \"${ServiceAccount}:(OI)(CI)M\")",
        "@($EnvFile, '/grant', \"${ServiceAccount}:R\")",
        "Set-AuthDirAcl",
    ):
        assert line in grants, line
    assert "LocalSystem" not in _function_body(text, "Install-HomeSenseService")
    assert "$account = $ServiceAccount" in _function_body(text, "Set-AuthDirAcl")


def test_new_env_disables_windows_toasts_for_the_service():
    body = _function_body(_text("install.ps1"), "Initialize-Configuration")
    assert "Set-DotEnvValue -EnvFile $EnvFile -Name 'HOMESENSE_WINDOWS_TOAST' -Value 'false'" in body


def test_grafana_zip_service_executable_is_quoted():
    body = _function_body(_text("install.ps1"), "Install-GrafanaFromZip")
    assert '<executable>"$(ConvertTo-XmlText $server)"</executable>' in body


def test_auth_dir_acl_applied_before_admin_account_is_written():
    """The elevated installer (Administrators) writes auth.db only after the folder is protected."""
    text = _text("install.ps1")
    assert re.search(r"^#Requires -RunAsAdministrator", text, re.MULTILINE)
    main = text[text.index("# --- main ---") :]
    assert main.index("\nInitialize-Configuration") < main.index("\nInitialize-Database")
    body = _function_body(text, "Initialize-Database")
    assert body.index("init-db") < body.index("Set-AuthDirAcl") < body.index("create-user")


def test_env_file_stays_in_install_dir():
    text = _text("install.ps1")
    assert "$EnvFile = Join-Path $InstallDir '.env'" in text
    assert "Join-Path $DataDir '.env'" not in text and "Join-Path $AuthDir '.env'" not in text


def test_data_dir_acl_is_locked_by_sid():
    text = _text("install.ps1")
    assert "/inheritance:r" in text
    for sid in ("S-1-5-18", "S-1-5-32-544"):  # SYSTEM, Administrators
        assert sid in text
    assert re.search(r"SetEnvironmentVariable\(\s*['\"]HOMESENSE_DATA_DIR['\"].*['\"]Machine['\"]", text)


def test_install_dir_is_locked_before_anything_is_created_in_it():
    """C:\\HomeSense would inherit C:\\'s ACL (Authenticated Users: Modify) -> SYSTEM code execution."""
    text = _text("install.ps1")
    main = text[text.index("# --- main ---") :]
    # Administrators + SYSTEM Full, Users RX; no installing-user grant (re-runs are elevated).
    lock = main.index("Set-PrivateAcl $InstallDir -ReadExecuteSids @($SidUsers) -NoInstallingUser")
    assert main.index("New-Item -ItemType Directory -Force -Path $InstallDir") < lock
    for step in ("Install-PythonVenv", "Initialize-Configuration", "Install-HomeSenseService"):
        assert lock < main.index(f"\n{step}"), step
    assert "$SidUsers = 'S-1-5-32-545'" in text
    # AdGuard runs as LocalSystem: no installing-user or Users grant, or an unelevated user could
    # replace AdGuardHome.exe/yaml and get SYSTEM.
    adguard = _function_body(text, "Install-AdGuardHome")
    acl_calls = re.findall(r"^\s*Set-PrivateAcl \$adGuardDir.*$", adguard, re.MULTILINE)
    assert len(acl_calls) == 1
    assert re.match(r"^\s*Set-PrivateAcl \$adGuardDir -NoInstallingUser(\s+#.*)?$", acl_calls[0]), acl_calls[
        0
    ]
    assert "-ReadExecuteSids" not in acl_calls[0]
    assert adguard.index("Set-PrivateAcl $adGuardDir") < adguard.index("'-s', 'install'")


@needs_powershell
def test_private_acl_arguments_for_install_dir():
    out = _run_install_function(
        "$SidSystem = 'S-1-5-18'; $SidAdministrators = 'S-1-5-32-544'; "
        "(Get-PrivateAclArguments -Path 'C:\\Home Sense' -Container "
        "-ReadExecuteSids @('S-1-5-32-545')) -join '|'",
        {},
    )
    assert out.split("|") == [
        "C:\\Home Sense",
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F",
        "*S-1-5-32-545:(OI)(CI)RX",
    ]
    body = _function_body(_text("install.ps1"), "Set-PrivateAcl")
    assert "@($Path, '/remove:g', \"*$userSid\")" in body


@needs_powershell
def test_private_acl_arguments_with_installing_user():
    out = _run_install_function(
        "$SidSystem = 'S-1-5-18'; $SidAdministrators = 'S-1-5-32-544'; "
        "(Get-PrivateAclArguments -Path 'C:\\d' -UserSid 'S-1-5-21-1-2-3-1001' -Container) -join '|'",
        {},
    )
    assert out.split("|")[-1] == "*S-1-5-21-1-2-3-1001:(OI)(CI)F"


@needs_powershell
def test_private_acl_arguments_for_file_have_no_inheritance_flags():
    out = _run_install_function(
        "$SidSystem = 'S-1-5-18'; $SidAdministrators = 'S-1-5-32-544'; "
        "(Get-PrivateAclArguments -Path 'C:\\x\\.env' -UserSid 'S-1-5-21-9') -join '|'",
        {},
    )
    assert out.split("|") == [
        "C:\\x\\.env",
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-18:F",
        "*S-1-5-32-544:F",
        "*S-1-5-21-9:F",
    ]


def test_service_reinstall_without_wrapper_falls_back_to_sc_delete():
    body = _function_body(_text("install.ps1"), "Install-HomeSenseService")
    assert "Test-Path -LiteralPath $ServiceExe" in body
    assert "Invoke-Native sc.exe @('delete', 'HomeSense')" in body


def test_adguard_admin_interface_is_loopback_only():
    for text in (_text("install.ps1"), README.read_text(encoding="utf-8")):
        admin_lines = [line for line in text.splitlines() if "nterfejs administratora" in line]
        assert admin_lines
        for line in admin_lines:
            assert "127.0.0.1" in line and "Wszystkie interfejsy" not in line, line


def test_grafana_failure_does_not_abort_firewall_and_tray():
    text = _text("install.ps1")
    main = text[text.index("# --- main ---") :]
    assert main.index("Set-FirewallRules") < main.index("Install-Grafana")
    assert main.index("New-TrayShortcut") < main.index("Install-Grafana")
    assert re.search(r"try \{\s*Install-Grafana\s*\} catch \{", main)


def test_tray_shortcut_uses_pythonw_and_create_user():
    text = _text("install.ps1")
    assert "create-user" in text
    body = _function_body(text, "New-TrayShortcut")
    assert "GetFolderPath('Startup')" in body
    assert "$shortcut.TargetPath = $VenvPythonw" in body
    assert "$shortcut.Arguments = '-m homesense.cli tray'" in body
    assert "$VenvPythonw = Join-Path $VenvDir 'Scripts\\pythonw.exe'" in text
    cli = (ROOT / "src" / "homesense" / "cli.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in cli


def test_dev_script_checks_python_version_before_venv():
    text = _text("dev.ps1")
    assert text.index("[version]'3.12'") < text.index("-m venv .venv")


def test_wheel_includes_package_data():
    """hatchling ships every file under src/homesense except git-ignored ones: none may be ignored."""
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/homesense"]
    assert not wheel.get("exclude") and not wheel.get("only-include")
    package = ROOT / "src" / "homesense"
    data = [p for p in package.rglob("*") if p.is_file() and p.suffix not in {".py", ".pyc"}]
    kinds = {p.relative_to(package).parts[:2] for p in data}
    for needed in (
        ("web", "templates"),
        ("web", "static"),
        ("storage", "migrations"),
        ("storage", "auth_migrations"),
    ):
        assert needed in kinds, needed
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("git not available")
    result = subprocess.run(
        [git, "check-ignore", "--no-index", *[str(p.relative_to(ROOT)) for p in data]],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "", f"git-ignored package data would be left out: {result.stdout}"


def _function_body(text: str, name: str) -> str:
    start = text.index(f"function {name}")
    following = text.find("\nfunction ", start + 1)
    return text[start : following if following != -1 else len(text)]


# --- dev.ps1 / uninstall.ps1 ---


def test_dev_script_runs_simulated_with_local_data_dir():
    text = _text("dev.ps1")
    assert '$env:HOMESENSE_MODE = "simulated"' in text or '$env:HOMESENSE_MODE="simulated"' in text
    assert re.search(r'\$env:HOMESENSE_DATA_DIR\s*=\s*"\.\\data-dev"', text)
    assert ".[dev,desktop]" in text
    assert "simulate --days 8" in text
    assert "init-db" in text and "serve" in text


def test_uninstall_keeps_data_unless_asked():
    text = _text("uninstall.ps1")
    assert re.search(r"\[switch\]\s*\$RemoveData", text)
    assert "-s uninstall" in text
    assert "Remove-NetFirewallRule" in text
    # data\ (including data\auth\) and .env are removed only inside the -RemoveData branch.
    branch = text[text.index("if ($RemoveData) {") : text.index("} else {", text.index("if ($RemoveData) {"))]
    assert "'data'" in branch and "'.env'" in branch
    assert text.count("'data'") == 1 and text.count("'.env'") == 1
    assert "data\\auth" in text  # the kept-data message names the accounts folder


def test_data_dev_is_git_ignored():
    assert "data-dev/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


# --- WinSW service template ---


def _render_winsw(values: dict[str, str]) -> ET.Element:
    """Same substitution as install.ps1: placeholders replaced by XML-escaped values."""
    from xml.sax.saxutils import escape

    text = WINSW_TEMPLATE.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", escape(value, {'"': "&quot;", "'": "&apos;"}))
    assert "{{" not in text, "unrendered placeholder"
    return ET.fromstring(text)


WINSW_VALUES = {
    "EXECUTABLE": r"C:\Home & Sense\venv\Scripts\homesense.exe",
    "INSTALL_DIR": r"C:\Home & Sense",
    "DATA_DIR": r"C:\Home & Sense\data",
    "LOG_DIR": r"C:\Home & Sense\logs",
}


def test_winsw_template_is_well_formed_after_substitution():
    root = _render_winsw(WINSW_VALUES)
    assert root.tag == "service"
    assert root.findtext("id") == "HomeSense"
    # Quoted absolute path (WinSW hands it to Process.Start, which accepts a quoted file name).
    assert root.findtext("executable") == f'"{WINSW_VALUES["EXECUTABLE"]}"'
    account = root.find("serviceaccount")
    assert account is not None
    assert (account.findtext("domain"), account.findtext("user")) == ("NT SERVICE", "HomeSense")
    assert account.find("password") is None  # virtual accounts have no password
    assert root.findtext("arguments") == "serve"
    assert root.findtext("workingdirectory") == WINSW_VALUES["INSTALL_DIR"]
    assert root.findtext("logpath") == WINSW_VALUES["LOG_DIR"]
    log = root.find("log")
    assert log is not None and log.get("mode") == "roll-by-size"
    assert int(log.findtext("keepFiles")) >= 1
    env = {e.get("name"): e.get("value") for e in root.findall("env")}
    assert env["HOMESENSE_DATA_DIR"] == WINSW_VALUES["DATA_DIR"]
    assert root.find("onfailure").get("action") == "restart"


def test_winsw_template_placeholders_match_install_script():
    placeholders = set(re.findall(r"\{\{(\w+)\}\}", WINSW_TEMPLATE.read_text(encoding="utf-8")))
    assert placeholders == set(WINSW_VALUES)
    text = _text("install.ps1")
    for key in placeholders:
        assert re.search(rf"\b{key}\s*=", text), key


def test_install_renders_every_grafana_placeholder():
    text = _text("install.ps1")
    grafana = ROOT / "grafana"
    sources = [grafana / "grafana.ini.template", *sorted((grafana / "provisioning").rglob("*.yaml"))]
    found = set()
    for path in sources:
        found |= set(re.findall(r"\{\{(\w+)\}\}", path.read_text(encoding="utf-8")))
    assert found == {"DATA_DIR", "PROVISIONING_DIR", "DASHBOARDS_DIR", "DB_PATH"}
    for key in found:
        assert re.search(rf"\b{key}\s*=", text), key


# --- README ---


def test_readme_sections_in_polish():
    text = README.read_text(encoding="utf-8")
    headings = [line.lstrip("#").strip() for line in text.splitlines() if line.startswith("#")]
    for section in ("Instalacja", "Prywatność", "Rozwiązywanie problemów"):
        assert any(h.startswith(section) for h in headings), section


def test_readme_covers_security_and_setup_details():
    text = README.read_text(encoding="utf-8")
    for needle in (
        "3001",
        "setup-token.txt",
        "http://127.0.0.1:8080/setup",
        "auth.db",
        "homesense.db",
        "HOMESENSE_SECRET_KEY",
        "dev.ps1",
        "counts_for_presence",
        "BotFather",
        "ntfy",
        "Dodaj do ekranu głównego",
        "DHCP Server",
        "Primary DNS",
        "LocalService",
        "CLAUDE.md",
    ):
        assert needle in text, needle


def test_readme_never_shows_setup_token_in_url():
    text = README.read_text(encoding="utf-8")
    assert not re.search(r"/setup\?", text)

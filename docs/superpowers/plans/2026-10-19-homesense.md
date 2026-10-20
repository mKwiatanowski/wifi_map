# HomeSense Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Domowy system monitoringu własnej sieci: obecność domowników po urządzeniach, wzorce i anomalie, DNS/ruch per urządzenie przez AdGuard Home, panel web + PWA + tray, Grafana osadzona w panelu, instalacja natywna na Windows.

**Architecture:** Jeden proces Python (`homesense serve`): kolektory (router TP-Link, ARP, AdGuard, symulator) → `Ingest` → SQLite (WAL) → `PresenceEngine` → profile/detektory → `AlertService` → notifiery. FastAPI serwuje JSON API, strony Jinja2+HTMX, SSE i reverse proxy do Grafany. Tray to osobny proces.

**Tech Stack:** Python ≥3.12 (dev 3.14), FastAPI, Uvicorn, Jinja2, HTMX/Alpine/Chart.js (vendored), SQLAlchemy 2 Core + SQLite, pydantic-settings, httpx, tplinkrouterc6u, pytest/pytest-asyncio/respx, ruff, pystray/pywebview (extra), WinSW, AdGuard Home, Grafana + frser-sqlite-datasource.

**Spec:** `docs/superpowers/specs/2026-10-19-homesense-design.md` (czytaj razem z `CLAUDE.md`).

## Global Constraints

- Python `>=3.12`; pakiet `homesense` w układzie `src/`; entry point `homesense = "homesense.cli:main"`.
- Kod/identyfikatory/komentarze/commity po angielsku; teksty UI i README po polsku z poprawnymi znakami diakrytycznymi.
- Czas: zawsze przez `Clock`; w bazie UTC jako tekst `'%Y-%m-%d %H:%M:%S'` (funkcje `ts_to_db`/`ts_from_db`); profile i prezentacja w `Settings.timezone` (`Europe/Warsaw`).
- MAC zawsze znormalizowany: małe litery, separator `:` (`normalize_mac`).
- SQL wyłącznie w `src/homesense/storage/`. Zmiana schematu = nowy plik `storage/migrations/NNNN_name.sql`.
- Testy nie dotykają sieci ani prawdziwego routera (fake klienci, `respx`, `FakeClock`). Na laptopie dev działa tylko tryb `simulated`.
- Porty: aplikacja `8080`, Grafana `127.0.0.1:3000`, AdGuard web `3001`, AdGuard DNS `53`.
- Retencja domyślnie 30 dni; `departure_timeout` domyślnie 15 min.
- Prywatność: tylko metadane; `privacy_mode='stats_only'` ⇒ brak nazw domen w bazie dla urządzeń tej osoby.
- Sekrety tylko w `.env` (w `.gitignore`); `.env.example` bez wartości tajnych.
- Każda awaria kolektora/notifiera łapana i raportowana; proces nie może paść.
- Komendy weryfikacji: `pytest -q` oraz `ruff check .` muszą przechodzić po każdym tasku.

## Review Focus

1. Telefon na chwilę znika z WiFi (uśpienie) → brak fałszywego „wyszedł” przed upływem `departure_timeout` (test w Task 8).
2. Router niedostępny / złe hasło → kolektor `down` w `collector_status`, pozostałe kolektory działają dalej (test w Task 3).
3. Zapytanie DNS z IP bez znanego urządzenia (np. `127.0.0.1`, IPv6) → liczone w statystykach pod `mac='unknown'`, bez wyjątku (test w Task 7).
4. Zmiana czasu DST w Europe/Warsaw (2026-10-25 03:00→02:00) → profile liczone bez błędów, sloty po czasie lokalnym (test w Task 9).
5. Pierwsze uruchomienie z pustą bazą → wszystkie strony UI renderują się (200) i pokazują stany „brak danych” (test w Task 14).

---

## File Structure

```
pyproject.toml, .env.example, README.md
src/homesense/
  __init__.py, cli.py, app.py, scheduler.py
  core/{__init__,models,clock,config,events,registry,timeutil}.py
  storage/{__init__,db,retention}.py
  storage/migrations/0001_initial.sql
  storage/repositories/{__init__,persons,devices,observations,presence,dns,alerts,profiles,settings,users,collector_status}.py
  collectors/{__init__,base,router_tplink,arp_scan,adguard_dns,simulated}.py
  network/{__init__,adguard,analysis}.py
  ingest.py, vendor.py
  presence/{__init__,engine}.py
  patterns/{__init__,profiles,detectors}.py
  alerts/{__init__,service,rules,dispatcher}.py
  notifications/{__init__,base,log,windows_toast,telegram,ntfy}.py
  security.py
  api/{__init__,server,deps,auth,routes_api,routes_pages,sse,grafana_proxy}.py
  web/templates/*.html, web/static/{css,js,vendor,icons}/..., web/static/manifest.webmanifest, web/static/sw.js
  tray/{__init__,app}.py
grafana/provisioning/{datasources/homesense.yaml,dashboards/homesense.yaml}, grafana/dashboards/*.json, grafana/grafana.ini.template
scripts/{install.ps1,dev.ps1,uninstall.ps1}, scripts/winsw/HomeSense.xml.template
tests/... (mirror of src)
```

---

### Task 1: Project scaffold + core types

**Files:**
- Create: `pyproject.toml`, `.env.example`, `src/homesense/__init__.py`, `src/homesense/core/__init__.py`, `core/models.py`, `core/clock.py`, `core/config.py`, `core/events.py`, `core/registry.py`, `core/timeutil.py`, `tests/__init__.py`, `tests/conftest.py`, `tests/core/test_models.py`, `tests/core/test_clock.py`, `tests/core/test_events.py`, `tests/core/test_config.py`, `tests/core/test_timeutil.py`

**Interfaces (Produces):**
```python
# core/models.py
def normalize_mac(mac: str) -> str            # "AA-BB-cc:dd.EE:FF" -> "aa:bb:cc:dd:ee:ff"; ValueError if not 12 hex
def is_random_mac(mac: str) -> bool           # locally-administered bit (second hex digit in {2,6,a,e})
@dataclass(frozen=True) class DeviceObservation: ts: datetime; mac: str; source: str; online: bool = True;
    ip: str | None = None; hostname: str | None = None; connection: str = "unknown";  # wired|2g|5g|6g|unknown
    signal: int | None = None; up_kbps: float | None = None; down_kbps: float | None = None
@dataclass(frozen=True) class DnsQueryRecord: ts: datetime; client_ip: str; domain: str; blocked: bool = False;
    reason: str | None = None
Observation = DeviceObservation | DnsQueryRecord
@dataclass class Person: id: int; name: str; color: str | None; privacy_mode: str; created_at: datetime
@dataclass class Device: mac: str; person_id: int | None; label: str | None; hostname: str | None; vendor: str | None;
    ip: str | None; first_seen: datetime; last_seen: datetime; trusted: bool; counts_for_presence: bool;
    kind: str | None; notes: str | None
    @property display_name -> str  # label or hostname or vendor or mac
    @property is_random -> bool
@dataclass(frozen=True) class PresenceEvent: ts: datetime; person_id: int; kind: Literal["arrive","leave"]; mac: str | None = None
@dataclass class PresenceInterval: person_id: int; start_ts: datetime; end_ts: datetime | None
@dataclass class Alert: kind: str; severity: Literal["info","warning","critical"]; title: str; message: str; ts: datetime;
    person_id: int | None = None; mac: str | None = None; data: dict = field(default_factory=dict);
    dedup_key: str | None = None; id: int | None = None; acknowledged: bool = False
@dataclass class CollectorStatus: name: str; last_ok_ts: datetime | None; last_error_ts: datetime | None;
    last_error: str | None; consecutive_failures: int
# events (also in core/models.py)
@dataclass(frozen=True) class ObservationsBatch: observations: tuple[Observation, ...]
@dataclass(frozen=True) class NewDeviceEvent: mac: str; ts: datetime
@dataclass(frozen=True) class AlertRaised: alert: Alert

# core/clock.py
class Clock(Protocol): def now(self) -> datetime   # tz-aware UTC
class SystemClock: now()
class FakeClock: __init__(start: datetime); now(); advance(delta: timedelta); set(dt: datetime)

# core/timeutil.py
UTC = timezone.utc
def ts_to_db(dt: datetime) -> str      # aware -> UTC '%Y-%m-%d %H:%M:%S'; naive -> ValueError
def ts_from_db(s: str) -> datetime     # -> aware UTC
def to_local(dt: datetime, tz: str) -> datetime
def floor_hour(dt: datetime) -> datetime  # UTC hour floor

# core/events.py
class EventBus:
    def subscribe(self, event_type: type, handler: Callable[[Any], Awaitable[None]]) -> None
    def subscribe_all(self, handler) -> None
    def unsubscribe_all(self, handler) -> None
    async def publish(self, event: object) -> None   # sequential; handler exceptions logged, never raised

# core/registry.py
class Registry(Generic[T]): register(name: str, item: T) -> None (ValueError on duplicate); get(name) -> T; items() -> list[tuple[str,T]]; names() -> list[str]

# core/config.py  (pydantic-settings, env_prefix="HOMESENSE_", env_file=".env")
class Settings(BaseSettings):
    mode: Literal["live","simulated"] = "live"; data_dir: Path = Path("data"); host: str = "0.0.0.0"; port: int = 8080
    timezone: str = "Europe/Warsaw"; secret_key: str = "change-me"
    router_enabled: bool = True; router_host: str = "192.168.0.1"; router_username: str = "admin"; router_password: str = ""; router_interval_s: int = 60
    arp_enabled: bool = True; arp_subnet: str = "192.168.0.0/24"; arp_interval_s: int = 120
    adguard_enabled: bool = True; adguard_url: str = "http://127.0.0.1:3001"; adguard_username: str = ""; adguard_password: str = ""; adguard_interval_s: int = 30
    grafana_url: str = "http://127.0.0.1:3000"
    departure_timeout_min: int = 15; retention_days: int = 30
    windows_toast: bool = True; telegram_bot_token: str = ""; telegram_chat_id: str = ""; ntfy_url: str = "https://ntfy.sh"; ntfy_topic: str = ""
    quiet_hours_start: int = 23; quiet_hours_end: int = 7; sim_seed: int = 42
    @property db_path -> Path  # data_dir / "homesense.db"
```

- [ ] **Step 1: pyproject.toml**
```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "homesense"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115", "uvicorn[standard]>=0.30", "jinja2>=3.1", "python-multipart>=0.0.9",
  "itsdangerous>=2.2", "sqlalchemy>=2.0.30", "pydantic-settings>=2.4", "httpx>=0.27",
  "tplinkrouterc6u>=5.0", "mac-vendor-lookup>=0.1.12", "tzdata>=2024.1",
]
[project.optional-dependencies]
desktop = ["pystray>=0.19", "pillow>=10", "pywebview>=5"]
windows = ["winotify>=1.1"]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "respx>=0.21", "ruff>=0.6"]
[project.scripts]
homesense = "homesense.cli:main"
[tool.hatch.build.targets.wheel]
packages = ["src/homesense"]
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
[tool.ruff]
line-length = 110
target-version = "py312"
[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM"]
```
Create venv: `python -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev]"`. If `tplinkrouterc6u` or `mac-vendor-lookup` fails to install on 3.14, report it (do not silently drop).

- [ ] **Step 2: Write failing tests**
```python
# tests/core/test_models.py
import pytest
from homesense.core.models import normalize_mac, is_random_mac
def test_normalize_mac_variants():
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"
def test_normalize_mac_invalid():
    with pytest.raises(ValueError): normalize_mac("zz:bb")
def test_random_mac():
    assert is_random_mac("da:a1:19:00:00:01") and not is_random_mac("00:11:22:33:44:55")

# tests/core/test_clock.py
from datetime import datetime, timedelta, timezone
from homesense.core.clock import FakeClock
def test_fake_clock_advance():
    c = FakeClock(datetime(2026,1,1,tzinfo=timezone.utc)); c.advance(timedelta(minutes=5))
    assert c.now() == datetime(2026,1,1,0,5,tzinfo=timezone.utc)

# tests/core/test_timeutil.py
def test_roundtrip(): dt = datetime(2026,10,25,1,30,tzinfo=timezone.utc); assert ts_from_db(ts_to_db(dt)) == dt
def test_naive_rejected(): with pytest.raises(ValueError): ts_to_db(datetime(2026,1,1))
def test_to_local_dst(): assert to_local(datetime(2026,10,25,0,30,tzinfo=UTC),"Europe/Warsaw").hour == 2 and to_local(datetime(2026,10,25,1,30,tzinfo=UTC),"Europe/Warsaw").hour == 2

# tests/core/test_events.py
async def test_publish_typed_and_all_and_errors_swallowed():
    bus = EventBus(); got = []
    async def h(e): got.append(("t", e))
    async def a(e): got.append(("a", e))
    async def bad(e): raise RuntimeError("x")
    bus.subscribe(NewDeviceEvent, bad); bus.subscribe(NewDeviceEvent, h); bus.subscribe_all(a)
    ev = NewDeviceEvent(mac="aa:bb:cc:dd:ee:ff", ts=datetime.now(timezone.utc))
    await bus.publish(ev)
    assert got == [("t", ev), ("a", ev)]

# tests/core/test_config.py
def test_env_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMESENSE_MODE","simulated"); monkeypatch.setenv("HOMESENSE_DATA_DIR", str(tmp_path))
    s = Settings(_env_file=None); assert s.mode == "simulated" and s.db_path == tmp_path/"homesense.db"
```
(Add needed imports in real files.)

- [ ] **Step 3: Run** `pytest -q` → FAIL (modules missing).
- [ ] **Step 4: Implement** the modules exactly per Interfaces. `tests/conftest.py` provides fixtures: `utc_start` (`datetime(2026,10,19,6,0,tzinfo=UTC)`), `clock` (`FakeClock(utc_start)`), `settings` (`Settings(_env_file=None, mode="simulated", data_dir=tmp_path)`). `.env.example` lists every Settings field as `HOMESENSE_<NAME>=` with non-secret defaults and empty secrets.
- [ ] **Step 5: Run** `pytest -q; ruff check .` → PASS.
- [ ] **Step 6: Commit** `feat: project scaffold and core types`.

---

### Task 2: Storage — DB, migrations, repositories, retention

**Files:**
- Create: `storage/__init__.py`, `storage/db.py`, `storage/migrations/0001_initial.sql`, `storage/repositories/*.py` (one per aggregate + `__init__.py` with `Repos`), `storage/retention.py`, `tests/storage/test_db.py`, `tests/storage/test_repositories.py`, `tests/storage/test_retention.py`
- Modify: `tests/conftest.py` (add `db`, `repos` fixtures)

**Interfaces:**
- Consumes: Task 1 models, `ts_to_db`/`ts_from_db`.
- Produces:
```python
# storage/db.py
class Database:
    def __init__(self, path: Path)            # creates parent dir; engine sqlite:///path, check_same_thread=False
    engine: sqlalchemy.Engine                  # on connect: PRAGMA journal_mode=WAL; busy_timeout=5000; foreign_keys=ON
    def migrate(self) -> int                   # applies storage/migrations/*.sql in order not yet in schema_version; returns current version
    def begin(self) -> ContextManager[Connection]   # engine.begin()
    def connect(self) -> ContextManager[Connection]

# storage/repositories/__init__.py
@dataclass class Repos: persons; devices; observations; presence; dns; alerts; profiles; settings; users; collector_status
    @classmethod def create(cls, db: Database) -> "Repos"

PersonRepo: create(name: str, color: str|None=None, privacy_mode: str="full") -> int; get(id) -> Person|None; list() -> list[Person];
    update(id, **fields) -> None (allowed: name,color,privacy_mode); delete(id) -> None (devices.person_id set NULL)
DeviceRepo: get(mac) -> Device|None; list() -> list[Device]; upsert_seen(mac, ts, ip=None, hostname=None, vendor=None) -> bool  # True if newly created; never overwrites non-null hostname/vendor with None; updates last_seen=max
    update(mac, **fields) (allowed: person_id,label,trusted,counts_for_presence,kind,notes); find_by_ip(ip) -> Device|None (most recent last_seen); for_person(person_id) -> list[Device]; seen_since(ts) -> list[Device]
ObservationRepo: add_many(obs: Iterable[DeviceObservation]) -> int; for_device(mac, since, until) -> list[DeviceObservation];
    online_buckets(mac, since, until, bucket_s: int = 60) -> list[datetime]   # sorted distinct bucket starts with online=1
    recent_traffic(mac, limit: int) -> list[DeviceObservation]  # newest first, rows with down_kbps not null
    delete_before(ts) -> int
PresenceRepo: add_event(e: PresenceEvent) -> int; events(person_id: int|None=None, since=None, until=None) -> list[PresenceEvent] (ascending);
    open_interval(person_id) -> PresenceInterval|None; start_interval(person_id, ts); close_interval(person_id, ts);
    intervals(person_id, since, until) -> list[PresenceInterval] (overlapping window); delete_before(ts) -> int
DnsRepo: add_hourly(hour_ts, mac, domain, queries: int, blocked: int) (UPSERT add); add_hourly_stats(hour_ts, mac, queries, blocked) (UPSERT add);
    mark_domain_seen(mac, domain, ts) -> bool (True if first); top_domains(mac: str|None, since, limit=20) -> list[tuple[str,int,int]]  # domain, queries, blocked
    hourly_stats(mac: str|None, since, until) -> list[tuple[datetime,str,int,int]]  # hour, mac, queries, blocked
    new_domains(since, mac: str|None=None) -> list[tuple[str,str,datetime]]  # mac, domain, first_seen
    top_blocked(since, limit=20) -> list[tuple[str,int]]; delete_before(ts) -> int
AlertRepo: add(a: Alert) -> int; exists_recent(dedup_key: str, since) -> bool; list(limit=100, unacknowledged_only=False) -> list[Alert] (newest first);
    acknowledge(id) -> None; acknowledge_all() -> None; count_unacknowledged() -> int; delete_before(ts) -> int
ProfileRepo: replace(subject_type: str, subject_id: str, values: dict[tuple[int,int], tuple[float,int]], updated_at) -> None;
    get(subject_type, subject_id) -> dict[tuple[int,int], tuple[float,int]]
SettingsRepo: get(key, default=None) -> Any (JSON); set(key, value) -> None
UserRepo: create(username, password_hash) -> int; get_by_username(username) -> tuple[int,str,str]|None; count() -> int; set_password(username, password_hash)
CollectorStatusRepo: record_ok(name, ts); record_error(name, ts, error: str); all() -> list[CollectorStatus]; get(name) -> CollectorStatus|None

# storage/retention.py
def purge_old(repos: Repos, now: datetime, retention_days: int) -> dict[str,int]   # deletes observations, presence events/closed intervals, dns, alerts older
```

- [ ] **Step 1: Migration `0001_initial.sql`** — exactly the schema from spec §5 (tables persons, devices, device_observations, presence_events, presence_intervals, dns_hourly, dns_hourly_stats, domain_first_seen, profiles, alerts, settings, users, collector_status, schema_version) with indexes `device_observations(mac, ts)`, `device_observations(ts)`, `presence_events(person_id, ts)`, `alerts(ts)`, `alerts(dedup_key, ts)`, `dns_hourly(hour_ts)`. `devices.person_id REFERENCES persons(id) ON DELETE SET NULL`. Timestamps TEXT.
- [ ] **Step 2: Failing tests** (key ones; write all):
```python
def test_migrate_idempotent(tmp_path):
    db = Database(tmp_path/"x.db"); assert db.migrate() == 1; assert db.migrate() == 1
def test_wal_enabled(db):
    with db.connect() as c: assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
def test_device_upsert_new_then_existing(repos, clock):
    assert repos.devices.upsert_seen("aa:bb:cc:dd:ee:01", clock.now(), ip="192.168.0.10", hostname="phone")
    clock.advance(timedelta(minutes=1))
    assert not repos.devices.upsert_seen("aa:bb:cc:dd:ee:01", clock.now(), ip="192.168.0.11")
    d = repos.devices.get("aa:bb:cc:dd:ee:01"); assert d.hostname == "phone" and d.ip == "192.168.0.11" and d.last_seen == clock.now()
def test_person_delete_unassigns_devices(repos, clock): ...
def test_online_buckets(repos, clock): # 3 obs within same minute + 1 next minute -> 2 buckets
def test_presence_intervals_open_close(repos, clock): ...
def test_dns_upsert_adds(repos, clock):
    h = floor_hour(clock.now()); repos.dns.add_hourly(h,"m","a.com",3,1); repos.dns.add_hourly(h,"m","a.com",2,0)
    assert repos.dns.top_domains("m", h) == [("a.com",5,1)]
def test_mark_domain_seen_first_only(repos, clock): ...
def test_alert_dedup_exists_recent(repos, clock): ...
def test_profiles_replace_and_get(repos, clock): ...
def test_settings_json_roundtrip(repos): repos.settings.set("k", {"a":[1,2]}); assert repos.settings.get("k") == {"a":[1,2]}
def test_collector_status(repos, clock): record_error twice -> consecutive_failures 2; record_ok -> 0
def test_retention_purges_old(repos, clock): add old (40 days) + new obs/alerts/dns; purge_old(..., 30) removes only old
```
- [ ] **Step 3: Run** → FAIL. **Step 4: Implement** using `sqlalchemy.text()` with named params; all datetimes via `ts_to_db`/`ts_from_db`; booleans as 0/1. **Step 5: Run** `pytest -q; ruff check .` → PASS. **Step 6: Commit** `feat: sqlite storage, migrations and repositories`.

---

### Task 3: Collector base and runner

**Files:**
- Create: `collectors/__init__.py`, `collectors/base.py`, `tests/collectors/test_runner.py`

**Interfaces:**
- Consumes: `Clock`, `CollectorStatusRepo`, `Observation`.
- Produces:
```python
class Collector(Protocol):
    name: str
    interval_s: float
    async def collect(self) -> list[Observation]
Sink = Callable[[list[Observation]], Awaitable[None]]
class CollectorRunner:
    def __init__(self, collectors: list[Collector], sink: Sink, status: CollectorStatusRepo, clock: Clock, max_backoff_s: float = 600)
    async def run_once(self, collector: Collector) -> bool         # True on success; exceptions caught -> status.record_error, log
    async def run_forever(self) -> None                            # one task per collector; sleep interval (or backoff = min(interval*2**failures, max_backoff)) ; cancel-safe
    def paused_until(self, name: str) -> datetime | None
    def pause(self, name: str, until: datetime) -> None             # run_once skips (returns True, no collect) while clock.now() < until
    def health(self) -> Literal["ok","degraded","down"]            # down: all enabled failing (consecutive_failures>=3); degraded: any >=3; else ok
```
- [ ] **Step 1: Failing tests**: success records ok and passes observations to sink; one collector raising (simulate wrong router password `PermissionError`) records error while other collector still delivers (Review Focus #2); `pause()` skips collect; `health()` transitions ok→degraded→down; `run_forever` cancellable (start, `await asyncio.sleep(0.05)` with interval 0.01, cancel, no exception leaks).
- [ ] **Step 2–4:** run fail → implement → pass. **Step 5: Commit** `feat: collector runner with backoff and health`.

---

### Task 4: Router (TP-Link) and ARP collectors

**Files:**
- Create: `collectors/router_tplink.py`, `collectors/arp_scan.py`, `tests/collectors/test_router_tplink.py`, `tests/collectors/test_arp_scan.py`, `tests/fixtures/arp_windows.txt`

**Interfaces:**
- Consumes: `Collector`, `DeviceObservation`, `normalize_mac`, `Clock`.
- Produces:
```python
class TplinkRouterCollector:
    name = "router"
    def __init__(self, host: str, username: str, password: str, clock: Clock, interval_s: float = 60,
                 client_factory: Callable[[], Any] | None = None)
    async def collect(self) -> list[DeviceObservation]
    # default client_factory: lambda: TplinkRouterProvider.get_client(f"http://{host}", password, username)
    # runs blocking calls in asyncio.to_thread: client.authorize(); status = client.get_status(); finally client.logout()
    # for d in status.devices: DeviceObservation(ts=now, mac=normalize_mac(d.macaddr), source="router", ip=d.ipaddr,
    #   hostname=d.hostname or None, connection=map_connection(d.type), signal=getattr(d,"signal",None),
    #   up_kbps / down_kbps = getattr(d,"up_speed"/"down_speed") * 8 / 1000 when not None (library reports bytes per second).
def map_connection(conn: Any) -> str   # Connection enum name contains "WIRED"->"wired", "2G"->"2g", "5G"->"5g", "6G"->"6g", else "unknown"

def parse_arp_output(text: str) -> list[tuple[str, str]]   # [(ip, mac)] for 'dynamic' rows (also Polish 'dynamiczny'); skip ff-ff-..., 01-00-5e-*, 33-33-*
class ArpScanCollector:
    name = "arp"
    def __init__(self, subnet: str, clock: Clock, interval_s: float = 120, runner: Callable[[list[str]], Awaitable[str]] | None = None, ping_concurrency: int = 64)
    async def collect(self) -> list[DeviceObservation]   # ping sweep all hosts of subnet (asyncio subprocess `ping -n 1 -w 500 ip`, semaphore), then `arp -a`; filter ips in subnet
```
- [ ] **Step 1: Fixture** `tests/fixtures/arp_windows.txt`:
```
Interface: 192.168.0.127 --- 0x7
  Internet Address      Physical Address      Type
  192.168.0.1           00-11-22-33-44-55     dynamic
  192.168.0.23          da-a1-19-00-00-01     dynamic
  192.168.0.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
Interfejs: 192.168.0.171 --- 0x12
  Adres internetowy     Adres fizyczny        Typ
  192.168.0.40          a4-83-e7-11-22-33     dynamiczny
```
- [ ] **Step 2: Failing tests**: parser returns 3 tuples with normalized macs; `ArpScanCollector` with fake `runner` (returns fixture for `arp -a`, "" for ping) returns observations only within subnet; router collector with fake client (object with `authorize`, `get_status` returning `SimpleNamespace(devices=[SimpleNamespace(macaddr="AA-BB-CC-DD-EE-01", ipaddr="192.168.0.10", hostname="iPhone", type=SimpleNamespace(name="HOST_5G"), signal=-55, up_speed=1000, down_speed=5000)])`, `logout`) → one observation with connection "5g", down_kbps 40.0; `logout` called even when `get_status` raises; exception propagates (runner handles).
- [ ] **Step 3–5:** fail → implement → pass; **Commit** `feat: tp-link router and arp collectors`.

---

### Task 5: AdGuard client + DNS collector

**Files:**
- Create: `network/__init__.py`, `network/adguard.py`, `collectors/adguard_dns.py`, `tests/network/test_adguard.py`, `tests/collectors/test_adguard_dns.py`, `tests/fixtures/adguard_querylog.json`

**Interfaces:**
- Produces:
```python
class AdGuardClient:
    def __init__(self, base_url: str, username: str = "", password: str = "", http: httpx.AsyncClient | None = None)
    async def status(self) -> dict                                # GET /control/status
    async def querylog(self, older_than: str | None = None, limit: int = 500) -> dict   # GET /control/querylog
    async def block_domain(self, domain: str) -> None             # read /control/filtering/status user_rules, add "||domain^" if absent, POST /control/filtering/set_rules {"rules":[...]}
    async def unblock_domain(self, domain: str) -> None           # remove "||domain^"
    async def block_client(self, ip: str) -> None                 # add rule "||*^$client='ip'" … use form: f"||*^$client={ip}"
    async def unblock_client(self, ip: str) -> None
    async def blocked_rules(self) -> list[str]
    async def aclose(self) -> None
    # basic auth if username set; raise AdGuardError(Exception) on non-2xx
def parse_querylog(payload: dict) -> list[DnsQueryRecord]
    # each item: time (RFC3339) -> ts aware UTC; client -> client_ip; question.name -> domain (lowercase, strip trailing '.');
    # blocked = reason startswith "Filtered" (e.g. FilteredBlackList, FilteredBlockedService, FilteredSafeBrowsing, FilteredParental); reason kept
class AdGuardDnsCollector:
    name = "adguard"
    def __init__(self, client: AdGuardClient, settings_repo: SettingsRepo, clock: Clock, interval_s: float = 30, max_pages: int = 20)
    async def collect(self) -> list[DnsQueryRecord]
    # cursor key "adguard.cursor" = last seen ts (db string). Page backwards using payload["oldest"] as older_than until an item ts <= cursor or empty or max_pages.
    # first run (no cursor): only take items from the last interval_s*2 seconds. Store new cursor = newest item ts. Return ascending by ts, excluding ts <= cursor.
```
- [ ] **Step 1: Fixture** `adguard_querylog.json`: `{"data":[{"time":"2026-10-19T08:00:05.123+02:00","client":"192.168.0.23","question":{"name":"api.spotify.com","type":"A"},"reason":"NotFilteredNotFound"},{"time":"2026-10-19T08:00:03+02:00","client":"192.168.0.40","question":{"name":"ads.example.","type":"A"},"reason":"FilteredBlackList"}],"oldest":"2026-10-19T08:00:03+02:00"}`
- [ ] **Step 2: Failing tests** (respx): parse_querylog → 2 records, second blocked, UTC ts 06:00:03; block_domain posts rules containing `||evil.com^` and keeps existing rules; unblock removes; non-2xx raises `AdGuardError`; collector first run respects window, second run returns only newer records and advances cursor.
- [ ] **Step 3–5:** fail → implement → pass; **Commit** `feat: adguard client and dns collector`.

---

### Task 6: Simulated household

**Files:**
- Create: `collectors/simulated.py`, `tests/collectors/test_simulated.py`

**Interfaces:**
- Produces:
```python
@dataclass class SimDevice: mac: str; ip: str; hostname: str; kind: str  # phone|laptop|tv|iot
@dataclass class SimPersona: name: str; devices: list[SimDevice]; weekday_away: tuple[float,float] | None; weekend_away: tuple[float,float] | None
    # local hours away (e.g. (7.5, 16.0)); None = home all day
def default_household() -> tuple[list[SimPersona], list[SimDevice]]   # 3 personas (Anna, Piotr, Ola) + shared devices (TV, 2 IoT, desktop PC)
class SimulatedHousehold:
    def __init__(self, clock: Clock, tz: str = "Europe/Warsaw", seed: int = 42, personas=None, shared=None)
    def is_home(self, persona: SimPersona, at: datetime) -> bool      # deterministic per (persona, date): jitter ±30 min via random.Random(hash((seed,name,date)))
    def device_observations(self) -> list[DeviceObservation]          # at clock.now(): phones of present personas online (but 10% of minutes "asleep" -> absent, deterministic), laptops online 18-23 when home, shared always; source="simulated"; down_kbps random 0-5000
    def dns_queries(self, window_s: float) -> list[DnsQueryRecord]    # per online device: Poisson-ish count by kind (phone 30/h, tv 60/h evening, iot 20/h) with domains from per-kind lists; ~5% ad domains blocked
    def inject_guest(self, at: datetime, minutes: int = 60) -> None   # unknown device "Guest-Phone" with random-looking MAC present in window
class SimulatedCollector:
    name = "simulated"; interval_s: float
    def __init__(self, household: SimulatedHousehold, interval_s: float = 30)
    async def collect(self) -> list[Observation]   # device_observations() + dns_queries(interval_s)
```
- [ ] **Step 1: Failing tests**: deterministic (same seed → same output at same time); weekday 10:00 local Anna (away 7.5–16) not home, 20:00 home; weekend differs; `dns_queries` only for online devices and includes some `blocked=True` over 1 h of samples; guest appears only inside injected window.
- [ ] **Step 2–4:** fail → implement → pass; **Commit** `feat: simulated household collector`.

---

### Task 7: Ingest + vendor lookup

**Files:**
- Create: `vendor.py`, `ingest.py`, `alerts/__init__.py`, `alerts/service.py`, `tests/test_ingest.py`, `tests/alerts/test_service.py`

**Interfaces:**
- Produces:
```python
# vendor.py
class VendorLookup:
    def __init__(self) ; def lookup(self, mac: str) -> str | None   # mac_vendor_lookup.MacLookup().lookup; any exception -> None; lru cache
# alerts/service.py
class AlertService:
    def __init__(self, repos: Repos, bus: EventBus, clock: Clock, dedup_window: timedelta = timedelta(hours=3))
    async def raise_alert(self, alert: Alert) -> Alert | None   # if dedup_key and repos.alerts.exists_recent(key, now-window): return None; else add, set id, publish AlertRaised, return alert
# ingest.py
UNKNOWN_MAC = "unknown"
class Ingest:
    def __init__(self, repos: Repos, bus: EventBus, alerts: AlertService, clock: Clock, vendor: Callable[[str], str | None])
    async def handle(self, observations: list[Observation]) -> None
    # DeviceObservation (online): upsert_seen(mac, ts, ip, hostname, vendor(mac) if new) ; if new -> publish NewDeviceEvent and raise_alert(Alert(kind="new_device", severity="warning",
    #    title="Nowe urządzenie w sieci", message=f"{hostname or vendor or mac} ({ip})", mac=mac, dedup_key=f"new_device:{mac}", data={"random_mac": is_random_mac(mac)}))
    #  add_many(all device obs)
    # DnsQueryRecord: mac = devices.find_by_ip(client_ip).mac or UNKNOWN_MAC; hour = floor_hour(ts); add_hourly_stats always;
    #    owner privacy: device.person_id -> person.privacy_mode; if "full" (or no owner, or unknown): add_hourly(hour, mac, domain, 1, int(blocked)) and mark_domain_seen
    #    aggregate in-memory per batch before writing (Counter) to minimize writes
    # finally publish ObservationsBatch(tuple(observations))
```
- [ ] **Step 1: Failing tests**: new device creates device + one `new_device` alert + NewDeviceEvent; repeated sighting → no second alert; DNS from IP of known device → `dns_hourly` row under its mac; DNS from `127.0.0.1` → only `dns_hourly_stats` under `"unknown"`… and also domain row under "unknown" (no owner ⇒ full) — assert no exception and stats exist (Review Focus #3); owner with `stats_only` → no `dns_hourly`/`domain_first_seen` rows but stats present; ObservationsBatch published once per handle; AlertService dedup within window returns None.
- [ ] **Step 2–4:** fail → implement → pass; **Commit** `feat: ingest pipeline and alert service`.

---

### Task 8: Presence engine

**Files:**
- Create: `presence/__init__.py`, `presence/engine.py`, `tests/presence/test_engine.py`

**Interfaces:**
- Produces:
```python
@dataclass class PersonState: person_id: int; home: bool; since: datetime | None; via_mac: str | None
class PresenceEngine:
    def __init__(self, repos: Repos, bus: EventBus, alerts: AlertService, clock: Clock, departure_timeout: timedelta)
    def restore(self) -> None                       # state from open intervals (home=True, since=start_ts) else home=False
    async def tick(self) -> list[PresenceEvent]     # for each person: devices with counts_for_presence; last = max(last_seen); home_now = last is not None and now-last <= timeout
        # away->home: event arrive at ts=last, start_interval(last). home->away: event leave at ts=last (last seen time), close_interval(last).
        # persist via repos.presence.add_event, publish PresenceEvent; raise_alert(Alert(kind="arrive"/"leave", severity="info", title=f"{name} wrócił(a) do domu"/f"{name} wyszedł/wyszła", person_id, dedup_key=None))
    async def on_batch(self, batch: ObservationsBatch) -> None   # await self.tick()
    def states(self) -> list[PersonState]
```
- [ ] **Step 1: Failing tests**: phone seen → arrive; phone missing 10 min (timeout 15) → still home, no leave (Review Focus #1); missing 16 min → leave with ts = last_seen; second device keeps person home; person without presence devices → never home; restore() after restart keeps open interval and does not emit duplicate arrive; arrive/leave alerts raised.
- [ ] **Step 2–4:** fail → implement → pass; **Commit** `feat: presence engine with departure hysteresis`.

---

### Task 9: Pattern profiles

**Files:**
- Create: `patterns/__init__.py`, `patterns/profiles.py`, `tests/patterns/test_profiles.py`

**Interfaces:**
- Produces:
```python
SLOTS = [(wd, h) for wd in range(7) for h in range(24)]
class ProfileBuilder:
    def __init__(self, repos: Repos, clock: Clock, tz: str, window_days: int = 30, min_samples: int = 2)
    def build_person_home(self, person_id: int) -> dict[tuple[int,int], tuple[float,int]]
        # iterate minutes in [now-window, now) stepping 60 s? -> too slow; instead: for each local hour slot instance in window,
        # fraction of that hour covered by presence intervals (clip). samples = number of hour instances observed since first data (first presence event or device first_seen).
        # keep slots with samples>=min_samples
    def build_device_active(self, mac: str) -> dict   # fraction of minutes online per hour instance using online_buckets(bucket_s=60) / 60
    def build_device_dns(self, mac: str) -> dict      # mean queries per hour instance from dns hourly_stats
    def rebuild_all(self) -> None                     # persons -> "person_home", devices seen in window -> "device_active","device_dns"; repos.profiles.replace(...)
def typical_times(events: list[PresenceEvent], tz: str) -> dict[int, dict[str, tuple[str, str] | None]]
    # weekday -> {"arrive": (median "HH:MM", iqr "H:MM"), "leave": ...} using local time-of-day minutes; None when < 2 samples
```
Hour instances are iterated in UTC hours and mapped to local (weekday, hour) via `to_local` — this makes DST safe (the repeated 02:00 local hour on 2026-10-25 simply contributes two samples to slot (6,2)).
- [ ] **Step 1: Failing tests**: person home every weekday 18–24 for 14 days → slot (0,20) value≈1.0, (0,10)≈0.0, samples≥2; insufficient data → empty dict; window spanning 2026-10-25 DST change builds without error and slot (6,2) has samples counted (Review Focus #4); device_active from observations; typical_times median arrive.
- [ ] **Step 2–4:** fail → implement → pass; **Commit** `feat: weekly pattern profiles`.

---

### Task 10: Anomaly detectors

**Files:**
- Create: `patterns/detectors.py`, `tests/patterns/test_detectors.py`

**Interfaces:**
- Consumes: Repos, ProfileBuilder outputs in `repos.profiles`, `PresenceEngine.states()`.
- Produces:
```python
@dataclass class DetectorContext: repos: Repos; now: datetime; tz: str; presence: list[PersonState]
class Detector(Protocol): name: str; def evaluate(self, ctx: DetectorContext) -> list[Alert]
class UnusualPresence:     name="unusual_presence"   # slot profile with samples>=4: home & p<0.1 or away & p>0.9 ; dedup f"unusual_presence:{pid}:{now:%Y%m%d%H}" // severity warning
class UnusualArrival:      name="unusual_arrival"    # last presence event within past 10 min outside median±max(2*IQR,60min) for weekday (>=4 samples) ; dedup per event ts
class NightActivity:       name="night_activity"     # local hour 1..5, device dns queries in last full hour > 20 and device_dns profile value for slot < 0.1*… (profile value < 5) ; dedup per mac+date
class DnsSpike:            name="dns_spike"          # last full hour queries > mean+4σ of same device over window and > 200
class BlockedBurst:        name="blocked_burst"      # last full hour blocked > 50 for a device
class NewDomainBurst:      name="new_domain_burst"   # device kind=="iot" with >10 new domains in last hour
class TrafficSpike:        name="traffic_spike"      # last 3 recent_traffic down_kbps all > mean+4σ of last 200 samples (>=50 samples)
class UnknownDevicePresent:name="unknown_device_present"  # device person_id None, trusted False, first_seen <= now-30min, last_seen >= now-5min; dedup per mac+date ; severity warning
def default_detectors() -> list[Detector]
async def run_detectors(detectors, ctx, alerts: AlertService) -> int   # evaluate each in try/except (log), raise_alert each; returns count raised
```
All titles/messages in Polish (e.g. „Nietypowa obecność”, „Aktywność w nocy”, „Skok zapytań DNS”, „Dużo zablokowanych zapytań”, „Nieznane urządzenie w sieci”).
- [ ] **Step 1: Failing tests** — one positive + one negative case per detector with hand-built repo data; a detector raising does not stop others in `run_detectors`.
- [ ] **Step 2–4:** fail → implement → pass; **Commit** `feat: anomaly detectors`.

---

### Task 11: Alert rules, dispatcher, notifiers

**Files:**
- Create: `alerts/rules.py`, `alerts/dispatcher.py`, `notifications/__init__.py`, `notifications/base.py`, `notifications/log.py`, `notifications/windows_toast.py`, `notifications/telegram.py`, `notifications/ntfy.py`, `tests/alerts/test_rules.py`, `tests/alerts/test_dispatcher.py`, `tests/notifications/test_notifiers.py`

**Interfaces:**
- Produces:
```python
# notifications/base.py
class Notifier(Protocol): name: str; async def send(self, alert: Alert) -> None
# log.py LogNotifier(name="log"); windows_toast.py WindowsToastNotifier(name="windows") uses winotify if importable else no-op warning;
# telegram.py TelegramNotifier(bot_token, chat_id, http: httpx.AsyncClient|None) POST https://api.telegram.org/bot{token}/sendMessage {"chat_id","text": f"{emoji} {title}\n{message}"}
# ntfy.py NtfyNotifier(base_url, topic, http=None) POST {base_url}/{topic} content=message, headers Title (ASCII-safe: use RFC 2047 or 'X-Title' with utf-8 -> use JSON publish: POST base_url with json {"topic","title","message","priority"} )
# alerts/rules.py
DEFAULT_RULES = {"channels": {"new_device": ["log","windows","telegram","ntfy"], "arrive": ["log"], "leave": ["log"], "*": ["log","windows","telegram","ntfy"]},
                 "min_severity": "info", "quiet_hours": {"start": 23, "end": 7, "allow": ["critical"]}}
class AlertRules:
    def __init__(self, settings_repo: SettingsRepo, defaults: dict, tz: str)
    def config(self) -> dict                          # settings "alerts.rules" merged over defaults
    def save(self, cfg: dict) -> None
    def channels_for(self, alert: Alert, now: datetime) -> list[str]   # kind lookup or "*"; severity filter; quiet hours (local, wraps midnight) -> only if severity in allow, "log" always kept
# alerts/dispatcher.py
class NotificationDispatcher:
    def __init__(self, rules: AlertRules, notifiers: Registry[Notifier], clock: Clock)
    async def on_alert(self, event: AlertRaised) -> None   # for each channel present in registry: try send, except log
    async def send_test(self, channel: str) -> None
```
Only notifiers with config present are registered (in Task 12): telegram if token+chat id, ntfy if topic, windows if `windows_toast` and platform win32.
- [ ] **Step 1: Failing tests**: quiet hours at 01:00 local → warning only "log", critical all; wrap-around midnight; kind-specific channels; dispatcher calls only registered notifiers and survives one raising; telegram/ntfy payloads via respx.
- [ ] **Step 2–4:** fail → implement → pass; **Commit** `feat: alert rules and notifiers`.

---

### Task 12: App composition, scheduler, security, CLI

**Files:**
- Create: `scheduler.py`, `security.py`, `app.py`, `cli.py`, `tests/test_app.py`, `tests/test_cli.py`, `tests/test_security.py`, `tests/test_simulation_integration.py`

**Interfaces:**
- Produces:
```python
# security.py
def hash_password(pw: str) -> str      # "scrypt$<salt_hex>$<hash_hex>" hashlib.scrypt n=2**14 r=8 p=1 dklen=64
def verify_password(pw: str, stored: str) -> bool   # hmac.compare_digest
# scheduler.py
class Periodic:  # run async fn every interval seconds, exceptions logged
    def __init__(self, name: str, interval_s: float, fn: Callable[[], Awaitable[None]])
    async def run_forever(self) -> None
# app.py
class HomeSenseApp:
    def __init__(self, settings: Settings, clock: Clock | None = None, http: httpx.AsyncClient | None = None)
    settings; clock; db; repos; bus; alerts: AlertService; ingest: Ingest; presence: PresenceEngine; profiles: ProfileBuilder;
    detectors: list[Detector]; rules: AlertRules; notifiers: Registry[Notifier]; dispatcher; runner: CollectorRunner;
    adguard: AdGuardClient | None; household: SimulatedHousehold | None
    def build_collectors(self) -> list[Collector]   # simulated mode -> [SimulatedCollector]; live -> router/arp/adguard by *_enabled
    async def start(self) -> None   # db.migrate(), presence.restore(), bus subscriptions (ObservationsBatch->presence.on_batch, AlertRaised->dispatcher.on_alert), create tasks:
                                    # runner.run_forever, Periodic presence tick 30s, profiles.rebuild_all 3600s (also once at start), detectors 300s, retention 86400s
    async def stop(self) -> None    # cancel tasks, close http/adguard
    async def step(self, collect: bool = True) -> None   # one synchronous cycle (used by simulate & tests): run_once each collector, presence.tick
    def detector_context(self) -> DetectorContext
# cli.py (argparse)  main(argv: list[str] | None = None) -> int
#  serve [--simulated] [--host] [--port]   -> uvicorn.run(create_app(settings)) (create_app from Task 13; import lazily)
#  init-db                                  -> migrate, print version
#  create-user USERNAME [--password]        -> prompts with getpass if not given; updates if exists
#  simulate --days N [--step-s 60]          -> settings.mode=simulated, FakeClock(now - N days), loop step()+advance until now; then profiles.rebuild_all and run_detectors once; inject one guest on the last day; print summary counts
#  doctor                                   -> checks DB, router login (live), AdGuard /control/status, Grafana /api/health, prints table OK/FAIL; exit 1 if any FAIL
#  tray                                     -> homesense.tray.app.main(settings) (lazy import)
```
- [ ] **Step 1: Failing tests**: hash/verify roundtrip and wrong password; `HomeSenseApp(settings(simulated), FakeClock)` + 1 person with Anna's phone assigned (`counts_for_presence=True`) + 30 `step()`s advancing 1 min during home hours → Anna home, devices > 0, dns stats > 0; `cli.main(["init-db"])` returns 0 with `HOMESENSE_DATA_DIR=tmp`; `cli.main(["create-user","admin","--password","x"])` then verify; **integration** `simulate --days 8 --step-s 300` completes (< 60 s), profiles table non-empty for a person once persons are auto-created by simulate (simulate creates persons Anna/Piotr/Ola and assigns their sim devices if not existing), and at least one `new_device` alert for the guest.
- [ ] **Step 2–4:** fail → implement → pass; **Commit** `feat: app composition, scheduler and cli`.

---

### Task 13: Web API, auth, SSE, Grafana proxy

**Files:**
- Create: `api/__init__.py`, `api/server.py`, `api/deps.py`, `api/auth.py`, `api/routes_api.py`, `api/sse.py`, `api/grafana_proxy.py`, `tests/api/test_auth.py`, `tests/api/test_routes_api.py`, `tests/api/test_sse.py`, `tests/api/test_grafana_proxy.py`

**Interfaces:**
- Produces:
```python
# api/server.py
def create_app(settings: Settings, core: HomeSenseApp | None = None, start_background: bool = True) -> FastAPI
    # lifespan: core.start() if start_background; app.state.core = core; SessionMiddleware(secret_key, same_site="lax", https_only=False);
    # static mount /static; include routers; templates = Jinja2Templates(web/templates) stored app.state.templates
# api/deps.py
def get_core(request) -> HomeSenseApp; def require_user(request) -> str  # session["user"] else 401 for /api, redirect 303 to /login for pages
def csrf_token(request) -> str; def check_csrf(request) -> None   # header "X-CSRF-Token" or form field "csrf_token" must match session; 403
# api/auth.py: GET /login (page), POST /login (form username,password -> session["user"], redirect "/"), POST /logout; GET /setup when users.count()==0 -> create first user form
# api/routes_api.py (all require_user; mutating require csrf):
GET  /api/health -> {"status": runner.health(), "collectors":[...], "people_home": n}   # NO auth (tray uses it); expose no personal names
GET  /api/presence -> [{"person_id","name","color","home","since","via"}]
GET  /api/persons; POST /api/persons {name,color,privacy_mode}; PATCH /api/persons/{id}; DELETE /api/persons/{id}
GET  /api/persons/{id}/profile -> {"home": [[wd,h,value,samples]...], "typical": typical_times(...)}
GET  /api/persons/{id}/timeline?date=YYYY-MM-DD -> intervals for local day
GET  /api/devices?filter=all|online|new|unassigned|unknown ; PATCH /api/devices/{mac} {person_id,label,trusted,counts_for_presence,kind,notes}
GET  /api/devices/{mac}/history?hours=24 -> online buckets + traffic
GET  /api/network/top-domains?mac=&hours=24 ; GET /api/network/blocked?hours=24 ; GET /api/network/new-domains?hours=24 ; GET /api/network/hourly?mac=&hours=48
POST /api/network/block-domain {domain}; POST /api/network/unblock-domain {domain}; POST /api/network/block-device {mac}; POST /api/network/unblock-device {mac}
     -> 503 {"detail":"AdGuard niedostępny"} if core.adguard None or AdGuardError
GET  /api/alerts?unack=1 ; POST /api/alerts/{id}/ack ; POST /api/alerts/ack-all
GET  /api/settings/alerts -> rules.config(); PUT /api/settings/alerts ; POST /api/settings/test-notification {channel}
POST /api/router/pause {minutes:15} -> runner.pause("router", now+minutes)
# api/sse.py  GET /api/events/stream (auth) -> text/event-stream; subscribe_all on bus, push JSON {"type": cls name, "data": {...}} ; heartbeat comment every 15s; unsubscribe on disconnect
# api/grafana_proxy.py  ANY /grafana/{path:path} (auth) -> httpx stream to settings.grafana_url + "/grafana/" + path with query, forward method/body/headers (minus host, cookie), strip hop-by-hop; 502 page "Grafana niedostępna" on connect error
# network/analysis.py (create here): functions top_domains(repos, mac, since, limit), hourly(...), summary(repos, since) -> dict used by routes
```
- [ ] **Step 1: Failing tests** (`TestClient(create_app(settings, core, start_background=False))`, core built with FakeClock in simulated mode): `/api/health` without login 200; `/api/persons` without login 401; setup→login flow; POST without CSRF 403, with 200; PATCH device assigns person; block-domain with respx-mocked AdGuard OK and 503 when AdGuard down; SSE receives a published `PresenceEvent` (use `client.stream` and publish in background via `anyio`, read first data line); grafana proxy forwards path/query (respx) and returns 502 on `httpx.ConnectError`.
- [ ] **Step 2–4:** fail → implement → pass; **Commit** `feat: web api, auth, sse and grafana proxy`.

---

### Task 14: Web UI pages + PWA

**Files:**
- Create: `api/routes_pages.py`, `web/templates/{base,login,setup,now,persons,person_detail,devices,device_detail,network,analytics,alerts,settings,privacy}.html`, partials `web/templates/partials/*.html`, `web/static/css/app.css`, `web/static/js/app.js`, `web/static/vendor/{htmx.min.js,alpine.min.js,chart.umd.min.js}`, `web/static/icons/{icon-192.png,icon-512.png,favicon.svg}`, `web/static/manifest.webmanifest`, `web/static/sw.js`, `tests/api/test_pages.py`

**Interfaces:**
- Pages (all require login, Polish UI): `/` (Teraz), `/persons`, `/persons/{id}`, `/devices`, `/devices/{mac}`, `/network`, `/analytics`, `/alerts`, `/settings`, `/privacy` („Co zbiera system”). Nav in `base.html` with unacked alert badge. HTMX partial refresh (`hx-get` every 15 s) for presence cards and event feed; `app.js` opens `EventSource('/api/events/stream')` and prepends events to feed; Chart.js heatmap 7×24 (matrix rendered as CSS grid — no plugin) on person page; bar/line charts on network & device pages. Forms send `X-CSRF-Token` via `hx-headers` from `<meta name="csrf-token">`.
- Analytics page: iframes `/grafana/d/homesense-presence?kiosk&theme=dark`, `homesense-devices`, `homesense-dns`, `homesense-traffic` with tabs; message when Grafana unavailable.
- Vendored libs: download `htmx.org@2` (`https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`), `alpinejs@3` (`https://unpkg.com/alpinejs@3.14.8/dist/cdn.min.js`), `chart.js@4` (`https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js`). Icons: generate PNGs with Pillow script (simple house glyph) — commit the PNGs.
- Design: clean dark/light (prefers-color-scheme) CSS variables, responsive (mobile first), cards, status dots; no external fonts.
- [ ] **Step 1: Failing tests**: with empty DB (fresh migrate, one user) every page returns 200 and contains its Polish heading and an empty-state text (Review Focus #5); with simulated data (a few `step()`s) `/` shows person names; unauthenticated page → 303 to `/login`; `/static/manifest.webmanifest` 200 JSON with `"display": "standalone"`; `/sw.js` served at root scope (route `/sw.js` returning the file with `Service-Worker-Allowed: /`).
- [ ] **Step 2–4:** fail → implement → pass. Manually run `homesense serve --simulated` after `homesense simulate --days 8` in a temp data dir and fetch `/` with curl after login to sanity check. **Commit** `feat: web ui pages and pwa`.

---

### Task 15: Grafana provisioning and dashboards

**Files:**
- Create: `grafana/grafana.ini.template`, `grafana/provisioning/datasources/homesense.yaml`, `grafana/provisioning/dashboards/homesense.yaml`, `grafana/dashboards/{presence,devices,dns,traffic}.json`, `tests/test_grafana_assets.py`

**Interfaces:**
- `grafana.ini.template` placeholders `{{DATA_DIR}}`, `{{PROVISIONING_DIR}}`: `[server] http_addr=127.0.0.1 http_port=3000 root_url=%(protocol)s://%(domain)s:%(http_port)s/grafana/ serve_from_sub_path=true`; `[security] allow_embedding=true`; `[auth.anonymous] enabled=true org_role=Viewer`; `[auth] disable_login_form=false`; `[paths] provisioning={{PROVISIONING_DIR}}`.
- Datasource: `type: frser-sqlite-datasource`, `uid: homesense-sqlite`, `jsonData.path: {{DB_PATH}}` (install script substitutes).
- Dashboards (uid `homesense-presence`, `homesense-devices`, `homesense-dns`, `homesense-traffic`), queries use `CAST(strftime('%s', ts) AS INTEGER) AS time` with `timeColumns: ["time"]`:
  - presence: state timeline per person from presence_intervals; arrivals/leaves table; heatmap of profiles person_home.
  - devices: online devices count over time (5-min buckets), table devices last_seen.
  - dns: queries per hour stacked by device, top domains bar, blocked ratio stat, new domains table.
  - traffic: down/up kbps per device timeseries.
- [ ] **Step 1: Failing test**: every dashboard JSON loads, has the expected uid, every panel target `datasource.uid == "homesense-sqlite"`, and every rawQuery SQL executes against a freshly migrated SQLite DB (strip Grafana macros: dashboards must not use macros other than `$__from`/`$__to` which test replaces with integers).
- [ ] **Step 2–4:** fail → implement → pass; **Commit** `feat: grafana provisioning and dashboards`.

---

### Task 16: Tray app

**Files:**
- Create: `tray/__init__.py`, `tray/app.py`, `tests/tray/test_tray_logic.py`

**Interfaces:**
```python
def health_color(status: str | None) -> tuple[int,int,int]   # ok green (46,160,67), degraded amber (210,153,34), down/None red (200,50,50)
def make_icon(color) -> PIL.Image.Image                      # 64x64 circle with house glyph
async def fetch_health(base_url: str, http: httpx.AsyncClient) -> dict | None   # GET /api/health timeout 3s; None on error
def open_window(url: str) -> None   # pywebview.create_window("HomeSense", url, width=1280, height=820); webview.start(); fallback webbrowser.open(url) if ImportError
def main(settings: Settings) -> None  # pystray Icon with menu: "Otwórz panel", "Otwórz w przeglądarce", "Wstrzymaj router 15 min" (needs login -> open /settings instead), "Zakończ"; thread polling health every 30s updating icon & title "HomeSense — N osób w domu"
```
Window must run in a separate process (`multiprocessing.Process(target=open_window, args=(url,))`) because pywebview needs the main thread.
- [ ] **Step 1: Failing tests**: `health_color` mapping; `fetch_health` with respx ok and error → None; `make_icon` returns 64×64 RGBA (skip if Pillow missing via `pytest.importorskip`).
- [ ] **Step 2–4:** fail → implement → pass; **Commit** `feat: system tray app`.

---

### Task 17: Windows install scripts + README

**Files:**
- Create: `scripts/install.ps1`, `scripts/uninstall.ps1`, `scripts/dev.ps1`, `scripts/winsw/HomeSense.xml.template`, `README.md`, `tests/test_scripts.py`

**Requirements:**
- `install.ps1` (param `-InstallDir C:\HomeSense`, `-SkipAdGuard`, `-SkipGrafana`; requires admin): Python check (≥3.12, else `winget install Python.Python.3.12`), venv, `pip install "<repo>[desktop,windows]"`, copy `.env.example` → `<InstallDir>\.env` if missing and open in notepad for router password; set `HOMESENSE_DATA_DIR`; `homesense init-db`; `homesense create-user` (interactive); download WinSW x64 (`https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe`) as `HomeSense.exe` + render XML (executable venv `homesense.exe serve`, workingdirectory InstallDir, log rotate) → `install` + `start`; AdGuard Home: download `https://static.adguard.com/adguardhome/release/AdGuardHome_windows_amd64.zip`, extract to `<InstallDir>\AdGuardHome`, `AdGuardHome.exe -s install`; the first-run wizard listens on port 3000, which Grafana also uses, so the script installs AdGuard first, opens `http://127.0.0.1:3000` and pauses (`Read-Host`-free: loop until `http://127.0.0.1:3001/control/status` answers, max 15 min) while the user completes the wizard setting admin web port **3001** and DNS port 53, and only then installs Grafana; Grafana: `winget install GrafanaLabs.Grafana.OSS` (fallback zip), `grafana-cli plugins install frser-sqlite-datasource`, render `grafana.ini.template` to Grafana `conf\custom.ini`, copy provisioning with `{{DB_PATH}}`, restart service `Grafana`; firewall rules `New-NetFirewallRule -Profile Private` for TCP 8080 and UDP/TCP 53; tray shortcut in `shell:startup` running `homesense.exe tray`; final summary with next steps (router DHCP DNS → target PC IP, DHCP reservation for the target PC, iPhone/Android „stały prywatny adres”).
- `uninstall.ps1`: stop/uninstall HomeSense service, optionally AdGuard (`-s uninstall`), remove firewall rules and startup shortcut; keep data unless `-RemoveData`.
- `dev.ps1`: venv, `pip install -e ".[dev,desktop]"`, `$env:HOMESENSE_MODE="simulated"; $env:HOMESENSE_DATA_DIR=".\data-dev"`, `homesense init-db`, `homesense simulate --days 8` if DB new, `homesense serve`.
- README.md (Polish): what it is, privacy notice for household, requirements, installation on the target PC step by step (incl. AdGuard wizard port 3001, router DNS setting screenshots description for TP-Link Archer-series routers: Advanced → Network → DHCP Server → Primary DNS), first configuration (create persons, assign devices, `counts_for_presence`), notifications setup (Telegram BotFather, ntfy), using the tray/PWA ("Dodaj do ekranu głównego"), troubleshooting (router single-session logout, DoH bypass, random MAC), development (dev.ps1, tests), extension pointers → CLAUDE.md.
- [ ] **Step 1: Failing test** `tests/test_scripts.py`: PowerShell scripts parse without syntax errors: run `powershell -NoProfile -Command "$null = [System.Management.Automation.Language.Parser]::ParseFile('<path>', [ref]$null, [ref]$errs); if ($errs) { exit 1 }"` for each `.ps1` (skip if not win32); WinSW XML template is well-formed after substitution; README contains sections "Instalacja", "Prywatność", "Rozwiązywanie problemów".
- [ ] **Step 2–4:** fail → implement → pass; **Commit** `feat: windows installer scripts and readme`.

---

### Task 18: End-to-end verification

- [ ] **Step 1:** fresh venv → `pip install -e ".[dev]"` → `pytest -q` (all pass) → `ruff check .`.
- [ ] **Step 2:** temp data dir: `homesense init-db; homesense create-user admin --password test; homesense simulate --days 8`; start `homesense serve --simulated --port 8099` in background; with `httpx` script: login (fetch csrf from /login form), GET each page → 200, GET `/api/presence`, `/api/network/top-domains` non-empty; stop server.
- [ ] **Step 3:** Fix anything found (new failing test first). **Commit** `chore: end-to-end verification fixes` if changes.

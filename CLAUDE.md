# HomeSense — instrukcje dla Claude

Domowy system monitoringu własnej sieci: obecność domowników (po urządzeniach), wzorce i anomalie, DNS/ruch per urządzenie (AdGuard Home), panel web + PWA + tray, Grafana osadzona w panelu. Pełny projekt: `docs/superpowers/specs/2026-10-19-homesense-design.md`. Decyzje: `docs/adr/`.

## Środowiska
- **Dev = komputer deweloperski.** Pracuj WYŁĄCZNIE w trybie symulowanym (`HOMESENSE_MODE=simulated`). Nie skanuj prawdziwej sieci, nie łącz się z routerem, nie zapisuj danych domowników na tym komputerze.
- **Prod = komputer docelowy (Windows 11, działający 24/7).** Instalacja `scripts/install.ps1`. Router: TP-Link obsługiwany przez bibliotekę `tplinkrouterc6u` (np. seria Archer); adres routera i podsieć ustawia się w `.env` (`HOMESENSE_ROUTER_HOST`, `HOMESENSE_ARP_SUBNET`; domyślnie fabryczne `192.168.0.1` i `192.168.0.0/24`). Przed routerem może stać terminal/modem operatora (bez API).
- Bez Dockera na Windows (ADR-0002): psuje przypisanie DNS do urządzeń i ARP.

## Komendy
```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"            # + ".[desktop]" dla tray/okna
.venv\Scripts\python -m pytest -q  # wszystkie testy (bez sieci, ≈2 min)
.venv\Scripts\ruff check .         # ruff format tylko na zmienianych plikach (nie całe repo)
homesense serve --simulated        # panel na http://127.0.0.1:8080 z symulowanym domem
homesense simulate --days 14       # wygeneruj 14 dni historii (przyspieszony czas)
homesense create-user admin        # konto do panelu
homesense doctor                   # sprawdź router/AdGuard/Grafana/DB
homesense tray                     # ikona w zasobniku + okno
.\scripts\dev.ps1                  # dev: venv + symulacja 8 dni (gdy nowa baza) + serve na 127.0.0.1:8080, dane w .\data-dev
# pierwsze konto: /setup + token z <data_dir>\auth\setup-token.txt (ważny 1 h, nigdy nie trafia do logów/URL)
# smoke testy na innym porcie i w tymczasowym data_dir — nie ruszaj serwera na 8080
```

## Mapa kodu (`src/homesense/`)
- `core/` — `config.Settings`, `clock.Clock` (zawsze używaj, nigdy `datetime.now()` bezpośrednio), `events.EventBus`, `models` (dataclasses), `registry` (wtyczki).
- `collectors/` — `base.Collector` + runner; `router_tplink`, `arp_scan`, `adguard_dns`, `simulated`.
- `storage/` — `db.py` (WAL, migracje `migrations/NNNN_*.sql`; auth.db: `auth_migrations/`), pakiet `repositories/` (plik na agregat), `auth.py`, `retention.py`.
- Dwie bazy SQLite: `<data_dir>\homesense.db` (dane; Grafana czyta ją tylko do odczytu) i `<data_dir>\auth\auth.db` (konta i hashe haseł, `storage/auth.py`; obok `setup-token.txt`). Folder `auth\` ma chroniony ACL (Grafana ma jawny zakaz). Ścieżki: `Settings.db_path` / `auth_db_path` / `auth_dir`. Na komputerze docelowym usługa działa jako `NT SERVICE\HomeSense`.
- `presence/radar.py` — radar urządzeń (strona `/map`, `GET /api/map`, `static/js/map.js`): odległość z RSSI (EMA + log-distance), kąt umowny z MAC, typ urządzenia (zapisany lub zgadnięty).
- `ingest.py` → `presence/engine.py` → `patterns/{profiles,detectors}.py` → `alerts/rules.py` → `notifications/`.
- `network/` — klient AdGuard i analiza DNS.
- `api/` — FastAPI (JSON `/api/*`, strony HTML, SSE, proxy `/grafana`, auth). `web/` — szablony Jinja2 + statyki (HTMX, Alpine, Chart.js vendored).
- `tray/` — pystray + pywebview. `app.py` — kompozycja. `cli.py` — komendy.
- `grafana/` — provisioning + dashboardy JSON. `scripts/` — install/dev PowerShell.

## Konwencje
- Kod, identyfikatory, komentarze, commity: **angielski**. Teksty UI, README, docs: **polski** (z poprawnymi znakami).
- Czas: UTC w bazie jako tekst `'%Y-%m-%d %H:%M:%S'` (tylko przez `core/timeutil.ts_to_db`/`ts_from_db`; naive datetime → ValueError), prezentacja w `Settings.timezone` (`Europe/Warsaw`).
- Testy: fixture'y w `tests/conftest.py` (`clock`=FakeClock, `settings`, `db`, `auth_db`, `repos`); struktura `tests/` odzwierciedla `src/`.
- Zależności przez konstruktor (bez globali); `app.py` składa całość. Moduły rozmawiają przez `core.models` i `EventBus`, nie importują się nawzajem „w poprzek”.
- SQL tylko w `storage/repositories/`. Zmiana schematu = nowy plik migracji, nigdy edycja starej.
- CSP bez `unsafe-inline` (`api/server.py`) — żadnych skryptów inline; nazwy urządzeń/hostname są kontrolowane przez atakującego → w JS tylko `textContent`, nigdy `innerHTML`.
- Nowe `/api/*` za `require_user`, mutacje z CSRF; `/api/health` dla anonimów zwraca tylko status (szczegóły: loopback albo sesja).
- Nigdy nie wstawiaj tekstu z sieci (hostname, domeny) do PowerShella/komend; subprocess tylko z listą argumentów.
- Grafana widzi tylko `homesense.db` w trybie read-only — sekrety nigdy w tej bazie (idą do `auth.db`).
- Kod musi się importować na Pythonie 3.12 (venv dev ma 3.14).
- I/O (HTTP, subprocess) tylko w kolektorach / `network/adguard.py` / notifierach — reszta czysta i testowalna.
- Każdy błąd kolektora/notifiera jest łapany, logowany i raportowany w `collector_status`; nie może zatrzymać procesu.
- TDD: najpierw test. Testy nie dotykają sieci (`respx`, fake klienci, `FakeClock`).

## Jak rozbudować
- **Kolektor**: nowa klasa w `collectors/` z `name`, `interval_s`, `async collect() -> list[Observation]`; dodaj w `HomeSenseApp.build_collectors()` (`app.py`) z flagą `*_enabled` w `Settings`. Nowy typ danych → nowy dataclass w `core/models.py` + migracja + repozytorium + obsługa w `ingest.py`. Planowane: `esp32_csi` (strefy/pokoje), `mmwave` (oddech/tętno).
- **Detektor anomalii**: klasa z `name` i `evaluate(ctx) -> list[Alert]` w `patterns/detectors.py`, ustaw `dedup_key`, dopisz test, dodaj do `default_detectors()`.
- **Kanał powiadomień**: klasa z `name` i `async send(alert)` w `notifications/`, pola konfiguracji w `Settings`/ustawieniach UI, rejestracja w `HomeSenseApp._build_notifiers()` (tylko gdy skonfigurowany).
- **Strona UI**: trasa w `api/routes_pages.py`, szablon w `web/templates/`, link w `base.html`, JS w osobnym pliku `web/static/js/` (dopisz do precache w `sw.js` i podbij wersję cache).
- **Dashboard Grafany**: JSON w `grafana/dashboards/` (datasource uid `homesense-sqlite`).

## Zasady prywatności (nie łamać)
- Tylko metadane (urządzenia, DNS, wolumen). Żadnego przechwytywania treści, MITM, łamania haseł.
- Respektuj `persons.privacy_mode='stats_only'` (bez nazw domen w bazie).
- Retencja domyślnie 30 dni. Sekrety tylko w `.env` (w `.gitignore`).

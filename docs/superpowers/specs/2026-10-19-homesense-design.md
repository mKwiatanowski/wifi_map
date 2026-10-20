# HomeSense — projekt systemu (spec)

Data: 2026-10-19 · Status: zaakceptowany (użytkownik zlecił realizację end-to-end)

## 1. Cel i kontekst

HomeSense to domowy system monitoringu **własnej sieci i własnego domu** właściciela. Odpowiada na pytania:

- kto z domowników jest teraz w domu i od kiedy (obecność na podstawie urządzeń),
- jakie są typowe wzorce dobowe/tygodniowe każdej osoby i kiedy dzieje się coś nietypowego,
- jakie urządzenia są w sieci, czy pojawiło się obce urządzenie,
- z czym łączą się urządzenia (DNS), ile transferują, co zostało zablokowane.

### Co powiedział użytkownik
- Użycie wyłącznie we własnym domu, we własnej sieci; kilku domowników; retencja 30 dni.
- Sprzęt: terminal/modem operatora (bez API) + router **TP-Link obsługiwany przez bibliotekę `tplinkrouterc6u` (np. seria Archer)** (brama, DHCP/DNS/WiFi dla LAN; adres ustawiany w `.env`, domyślnie fabryczny `192.168.0.1`).
- Bez dodatkowych czujników (brak ESP32/mmWave) w tej wersji.
- Development na komputerze deweloperskim, **uruchomienie na komputerze docelowym (Windows 11, działający 24/7)**.
- Wariant „najbardziej rozbudowany”: panel, powiadomienia, wzorce, ruch sieciowy + blokowanie.
- Hybryda: własna aplikacja + Grafana; dostęp przez przeglądarkę, okienko (tray) i PWA na telefonie.

### Założenia
- Domownicy wiedzą o systemie (wymóg prawny i etyczny) — aplikacja ma stronę „Co zbiera system” i tryb prywatności per osoba.
- Brak chmury: wszystkie dane lokalnie na komputerze docelowym.
- „Nadzór sieci” = metadane (urządzenia, DNS, wolumen). **Bez przechwytywania treści komunikacji**, bez MITM, bez łamania haseł.

### Poza zakresem (wersja 1)
- Parametry życiowe (oddech/tętno) i mapa pomieszczeń — wymagają czujników (ESP32 CSI / radar mmWave). Architektura przewiduje to jako przyszłe kolektory (sekcja 10).
- Docker na Windows (patrz ADR-0002).

### Kryteria sukcesu
1. Po instalacji na komputerze docelowym (`install.ps1`) panel pod `http://<komputer>:8080` pokazuje urządzenia z routera i status obecności w < 1 min.
2. Przypisanie urządzeń do osób w UI → poprawne zdarzenia przyjścia/wyjścia (opóźnienie wyjścia ≤ timeout, domyślnie 15 min).
3. Po ≥ 7 dniach danych widoczny profil tygodniowy osoby i alerty odchyleń.
4. Zapytania DNS przypisane do urządzeń (nie do jednego IP), top domen, blokowanie domeny/urządzenia z UI.
5. Powiadomienia (Windows toast, Telegram, ntfy) o nowym urządzeniu, przyjściu/wyjściu, anomalii.
6. Cały system działa w trybie `simulated` bez sieci (development na komputerze deweloperskim) i ma testy automatyczne.

## 2. Architektura

```
                     ┌──────────── komputer docelowy (Windows 11) ───────────┐
 TP-Link     ◄─HTTP──┤ collectors/router_tplink ─┐                           │
 LAN (ping/arp) ◄────┤ collectors/arp_scan ──────┼─► Observation ─► EventBus │
 AdGuard Home ◄─HTTP─┤ collectors/adguard_dns ───┘        │                   │
  (DNS :53)          │                                   ▼                   │
                     │  ingest ─► storage (SQLite WAL, homesense.db)          │
                     │     │          ▲            ▲                         │
                     │     ▼          │            │ (read-only)             │
                     │  presence ─► patterns ─► alerts ─► notifications       │
                     │                                                       │
                     │  api (FastAPI :8080) ── web (Jinja2+HTMX+SSE, PWA)    │
                     │        └── /grafana/* reverse proxy ─► Grafana :3000   │
                     │  tray (pystray + pywebview) ─► okno z panelem         │
                     └───────────────────────────────────────────────────────┘
```

Jeden proces aplikacji (`homesense serve`) uruchamia FastAPI + harmonogram kolektorów i zadań (asyncio). Tray to osobny lekki proces (`homesense tray`), który tylko odpytuje `/api/health` i otwiera okno.

Komponenty zewnętrzne (natywne usługi Windows): **AdGuard Home** (DNS + blokowanie + log zapytań, API REST), **Grafana** (dashboardy czytające SQLite przez wtyczkę `frser-sqlite-datasource`, nasłuch tylko 127.0.0.1, dostęp przez proxy aplikacji).

## 3. Stos technologiczny

| Obszar | Wybór |
|---|---|
| Język | Python ≥ 3.12 (dev: 3.14) |
| Web/API | FastAPI, Uvicorn, Jinja2, HTMX, Alpine.js (vendored, bez Node/buildu), Chart.js (vendored) |
| Live | Server-Sent Events (`/api/events/stream`) |
| DB | SQLite (tryb WAL), SQLAlchemy 2.0 Core, migracje: prosty runner SQL w `storage/migrations/NNNN_*.sql` |
| Config | pydantic-settings (`.env` + zmienne `HOMESENSE_*`) |
| Router | `tplinkrouterc6u` (obsługuje routery TP-Link, m.in. serię Archer) |
| DNS | AdGuard Home REST API (`/control/querylog`, `/control/clients`, `/control/filtering/*`, `/control/blocked_services/*`) przez `httpx` |
| HTTP klient | `httpx` (async) |
| Vendor MAC | lokalna baza OUI (`mac-vendor-lookup`, offline cache) |
| Powiadomienia | `win11toast`/`winotify` (opcjonalnie), Telegram Bot API, ntfy (httpx) |
| Tray/okno | `pystray`, `Pillow`, `pywebview` (extra `[desktop]`) |
| Testy | pytest, pytest-asyncio, respx (mock httpx), freezegun lub wstrzykiwany `Clock` |
| Jakość | ruff (lint + format) |
| Usługa Windows | WinSW (opakowanie `homesense serve` jako usługa) |

## 4. Moduły (`src/homesense/`)

Każdy moduł ma jedną odpowiedzialność i komunikuje się przez typy z `core/models.py` oraz `EventBus`.

| Moduł | Odpowiedzialność | Zależy od |
|---|---|---|
| `core/config.py` | `Settings` (pydantic-settings) | — |
| `core/clock.py` | `Clock` (real/fake) — cały kod pyta o czas przez Clock | — |
| `core/events.py` | `EventBus` (async pub/sub w procesie) + typy zdarzeń | models |
| `core/models.py` | dataclasses: `DeviceObservation`, `DnsQueryRecord`, `PresenceEvent`, `Alert`, … | — |
| `core/registry.py` | rejestry wtyczek: collectors, notifiers, detectors | — |
| `collectors/base.py` | protokół `Collector` (name, interval, `async collect()`) + runner z backoffem i statusem zdrowia | core |
| `collectors/router_tplink.py` | poll routera TP-Link → `DeviceObservation` | tplinkrouterc6u |
| `collectors/arp_scan.py` | ping sweep podsieci + odczyt tablicy ARP Windows (`arp -a`) | — |
| `collectors/adguard_dns.py` | przyrostowe pobieranie querylog → `DnsQueryRecord` | httpx |
| `collectors/simulated.py` | symulowany dom (persony z harmonogramami) → obserwacje + DNS | core |
| `storage/db.py` | engine, WAL, migracje | SQLAlchemy |
| `storage/repositories.py` | repozytoria: devices, persons, observations, presence, dns, alerts, profiles, settings | db |
| `storage/retention.py` | usuwanie danych > `retention_days` (domyślnie 30), `VACUUM` tygodniowo | repos |
| `ingest.py` | subskrybuje obserwacje → upsert urządzeń, zapis szeregów, wykrycie nowego urządzenia, agregacja DNS z uwzględnieniem prywatności | repos, bus |
| `presence/engine.py` | maszyna stanów obecności per osoba (histereza) → `PresenceEvent` | repos, bus, clock |
| `patterns/profiles.py` | profile tygodniowe 7×24 (p_home per osoba, aktywność per urządzenie) | repos |
| `patterns/detectors.py` | detektory anomalii (wtyczki) → `Alert` | profiles, registry |
| `network/adguard.py` | klient AdGuard (statystyki, klienci, blokowanie domen/urządzeń) | httpx |
| `network/analysis.py` | top domeny, nowe domeny, kategorie podejrzane, wolumen | repos |
| `alerts/rules.py` | reguły → które alerty wysłać którym kanałem, deduplikacja, cisza nocna | repos |
| `notifications/*.py` | `Notifier` protokół + windows_toast, telegram, ntfy, log | registry |
| `api/` | routery FastAPI: `/api/*` JSON, `/` strony HTML, SSE, `/grafana` proxy, auth | wszystko powyżej |
| `web/templates`, `web/static` | UI (PL), manifest PWA, service worker | — |
| `tray/app.py` | ikona w zasobniku + okno pywebview | httpx |
| `app.py` | kompozycja: tworzy obiekty, rejestruje wtyczki, startuje harmonogram | wszystko |
| `cli.py` | `homesense serve|tray|init-db|simulate|create-user|doctor` | app |

## 5. Model danych (SQLite)

```sql
persons(id PK, name UNIQUE, color, privacy_mode TEXT CHECK IN ('full','stats_only') DEFAULT 'full',
        created_at)
devices(mac PK, person_id FK NULL, label, hostname, vendor, ip, first_seen, last_seen,
        trusted BOOL DEFAULT 0, counts_for_presence BOOL DEFAULT 0, kind TEXT, notes)
device_observations(id PK, ts, mac, ip, source, online BOOL, connection TEXT,   -- wired|2g|5g|unknown
        signal INT NULL, up_kbps REAL NULL, down_kbps REAL NULL)        INDEX(mac, ts), INDEX(ts)
presence_events(id PK, ts, person_id, kind TEXT CHECK IN ('arrive','leave'), mac)
presence_intervals(id PK, person_id, start_ts, end_ts NULL)            -- do osi czasu / Grafany
dns_hourly(hour_ts, mac, domain, queries, blocked, PRIMARY KEY(hour_ts, mac, domain))
dns_hourly_stats(hour_ts, mac, queries, blocked, PRIMARY KEY(hour_ts, mac)) -- także dla stats_only
domain_first_seen(mac, domain, first_seen, PRIMARY KEY(mac, domain))
profiles(subject_type TEXT, subject_id TEXT, weekday INT, hour INT, value REAL, samples INT,
         updated_at, PRIMARY KEY(subject_type, subject_id, weekday, hour))
         -- subject_type: 'person_home' | 'device_active' | 'device_dns'
alerts(id PK, ts, kind, severity TEXT CHECK IN ('info','warning','critical'), person_id NULL,
       mac NULL, title, message, data_json, acknowledged BOOL DEFAULT 0, dedup_key)
settings(key PK, value_json)
collector_status(name PK, last_ok_ts, last_error_ts, last_error, consecutive_failures)
schema_version(version)
```

Osobna baza `<data_dir>\auth\auth.db` (nie `homesense.db`, bo tę czyta Grafana):

```sql
users(id PK, username UNIQUE, password_hash, created_at)              -- logowanie do panelu
```

Folder `<data_dir>\auth\` (także `setup-token.txt`) ma chroniony ACL: tylko administratorzy, SYSTEM i konto usługi `NT SERVICE\HomeSense`; Grafana ma jawny zakaz. Grafana otwiera `homesense.db` tylko do odczytu (`mode=ro`, `query_only`).

Wszystkie znaczniki czasu: UTC, ISO-8601 tekst (czytelne dla Grafany: `strftime`). Prezentacja w strefie `Europe/Warsaw` (konfigurowalne).

Szacunek rozmiaru (20 urządzeń, poll 30 s, 30 dni): obserwacje ~1,7 mln wierszy (~150 MB), DNS hourly ~ kilkaset tys. wierszy. SQLite WAL wystarcza.

## 6. Przepływ danych i algorytmy

### 6.1 Kolektory
- **router_tplink** (domyślnie co 60 s): `TplinkRouterProvider.get_client(host, password)` → `authorize()` → `get_status().devices` → `logout()` w `finally`. Uwaga: TP-Link dopuszcza jedną sesję admina — każdy poll się wylogowuje; w UI przycisk „wstrzymaj odpytywanie routera na 15 min” (żeby można było wejść do panelu routera).
- **arp_scan** (co 120 s): równoległy ping (`ping -n 1 -w 500`, max 64 naraz) po podsieci z configu, potem parsowanie `arp -a`. Wpisy `dynamic` z MAC ≠ broadcast/multicast → `DeviceObservation(source='arp', online=True)`.
- **adguard_dns** (co 30 s): `GET /control/querylog?older_than=&limit=500` stronicowane wstecz aż do ostatnio widzianego `time`; mapowanie IP→MAC przez najnowsze obserwacje urządzeń (tabela ip w `devices`). Kursor (ostatni czas) w `settings`.
- **simulated**: deterministyczny (seed) dom: 3–4 persony z telefonami/laptopami, harmonogram praca/szkoła/weekend, losowe odchylenia, urządzenia IoT, TV, okazjonalny „obcy” gość; generuje obserwacje i DNS zgodnie z `Clock` (może przyspieszać czas w `homesense simulate --days 14`).

Runner kolektorów: każdy w osobnym tasku, wyjątek → log + `collector_status` + wykładniczy backoff (max 10 min); nigdy nie zabija procesu. Status zdrowia agregowany w `/api/health` (ok / degraded / down) i kolorze ikony tray.

### 6.2 Ingest
- Upsert `devices` (first_seen, last_seen, ip, hostname, vendor z OUI). Nowy MAC → `Alert(kind='new_device', severity='warning')` + zdarzenie na bus.
- Zapis `device_observations`. Połączenie wielu źródeł: urządzenie online, jeśli którekolwiek źródło widziało je w ostatnim oknie.
- DNS: agregacja do `dns_hourly` (pomijane, jeśli osoba właściciela ma `privacy_mode='stats_only'`) i zawsze `dns_hourly_stats`; `domain_first_seen` (tylko tryb full).
- Losowe (prywatne) adresy MAC: bit „locally administered” → oznaczenie w UI z poradą „ustaw stały prywatny adres dla sieci domowej”.

### 6.3 Obecność
Dla każdej osoby: urządzenia z `counts_for_presence=1`. Stan `home` gdy którekolwiek widziane online w ciągu `departure_timeout` (domyślnie 15 min — telefony w uśpieniu znikają z WiFi). Przejście away→home natychmiast przy pierwszej obserwacji; home→away po przekroczeniu timeoutu (sprawdzane co 30 s przez tick). Zdarzenia → `presence_events`, `presence_intervals`, bus (SSE + reguły alertów). Stan odtwarzany po restarcie z ostatniego interwału.

### 6.4 Wzorce
Przeliczane co godzinę (i na żądanie):
- `person_home[weekday,hour]` = udział minut w domu w danym slocie, średnia z ostatnich 30 dni (min. 2 próbki na slot, inaczej brak profilu).
- `device_active[weekday,hour]` = udział minut online.
- `device_dns[weekday,hour]` = średnia liczba zapytań DNS/h.
- Typowe godziny przyjść/wyjść per osoba i dzień tygodnia: mediana i IQR z `presence_events`.

### 6.5 Detektory anomalii (wtyczki `Detector`)
| Detektor | Reguła (domyślne progi w configu) |
|---|---|
| `unusual_presence` | osoba w domu, gdy `p_home < 0.1` lub poza domem, gdy `p_home > 0.9` (slot z ≥ 4 próbkami); max 1 alert/osobę/3 h |
| `unusual_arrival` | przyjście/wyjście poza [mediana ± max(2·IQR, 60 min)] |
| `night_activity` | urządzenie aktywne (DNS > 20/h) w godzinach 1–5, gdy profil < 0.1 |
| `dns_spike` | zapytania/h > średnia + 4·σ i > 200 |
| `new_domain_burst` | urządzenie IoT (kind=iot) łączy się z > 10 nowymi domenami w godzinę |
| `blocked_burst` | > 50 zablokowanych zapytań/h z jednego urządzenia (możliwy malware/tracker) |
| `traffic_spike` | down/up_kbps > średnia + 4·σ przez 3 kolejne pomiary |
| `unknown_device_present` | nieprzypisane, nieufne urządzenie online > 30 min |

Detektor = klasa z `name`, `evaluate(ctx) -> list[Alert]`, rejestrowana w `registry.detectors`. `dedup_key` zapobiega powtórkom.

### 6.6 Alerty i powiadomienia
`alerts/rules.py`: konfiguracja (w `settings`, edytowalna w UI) mapuje `kind`/`severity` → kanały; cisza nocna (domyślnie 23–7 tylko `critical`); deduplikacja po `dedup_key` w oknie. Notifier = protokół `async send(alert)`; implementacje: `log` (zawsze), `windows_toast`, `telegram` (bot token + chat id), `ntfy` (url + topic). Błędy wysyłki logowane, nie przerywają pętli.

### 6.7 Sieć / AdGuard
- Klient AdGuard: status, klienci, `filtering/check_host`, dodawanie reguł użytkownika (`||domena^` przez `/control/filtering/set_rules`), blokada urządzenia (reguła `||*^$client=<ip>` lub persistent client z `blocked_services`/`use_global_settings`), odblokowanie.
- `network/analysis.py`: top domen per urządzenie/okres, nowe domeny (24 h), proporcja zablokowanych, sumaryczny ruch (z router up/down).
- Konfiguracja wymagana od użytkownika (opisana w README): w routerze → DHCP → Primary DNS = IP komputera docelowego; rezerwacja adresu IP dla komputera docelowego.

## 7. UI

Panel (Jinja2 + HTMX, po polsku, ciemny/jasny motyw, responsywny):

- **Teraz** — karty osób (w domu/poza, od kiedy), liczba urządzeń online, strumień zdarzeń (SSE), status kolektorów.
- **Osoby** — CRUD osób, tryb prywatności, profil 7×24 (heatmapa Chart.js), typowe godziny, oś czasu dnia, „dziś vs norma”.
- **Urządzenia** — lista z filtrem (online/nowe/nieprzypisane/obce), przypisanie do osoby, `counts_for_presence`, `trusted`, rodzaj, etykieta; historia online; ruch.
- **Sieć** — top domen, zablokowane, nowe domeny, wykres zapytań/h, akcje: zablokuj domenę / urządzenie, odblokuj.
- **Analityka** — osadzone dashboardy Grafany (iframe `/grafana/d/...?kiosk`).
- **Alerty** — lista, potwierdzanie, filtr.
- **Ustawienia** — kanały powiadomień (+ „wyślij test”), reguły, cisza nocna, progi, retencja, pauza routera.
- **Co zbiera system** — przejrzysty opis zbieranych danych dla domowników.

Bezpieczeństwo: logowanie (użytkownik + hasło, `argon2`/`bcrypt` przez `passlib` lub `hashlib.scrypt`), sesja cookie podpisana (`itsdangerous`), CSRF dla formularzy (token w sesji), nasłuch domyślnie `0.0.0.0:8080` tylko LAN (README: nie wystawiać na internet). Grafana: `http_addr=127.0.0.1`, `serve_from_sub_path=true`, `root_url=%(protocol)s://%(domain)s/grafana/`, `allow_embedding=true`, anonimowy Viewer; dostęp wyłącznie przez proxy `/grafana/*`, które wymaga sesji aplikacji.

PWA: `manifest.webmanifest`, ikony, `sw.js` (cache statyk, offline fallback). Tray: `pystray` ikona (zielona/żółta/czerwona wg `/api/health`), menu: „Otwórz panel” (okno `pywebview`), „Otwórz w przeglądarce”, „Wstrzymaj router 15 min”, „Zakończ tray”.

## 8. Instalacja i uruchomienie

- `pyproject.toml` (pakiet `homesense`, extras: `desktop`, `windows`, `dev`), entry point `homesense`.
- `scripts/install.ps1` (komputer docelowy, jako administrator): sprawdza Python, tworzy venv w `C:\HomeSense\venv`, instaluje pakiet, `homesense init-db`, `homesense create-user`, pobiera WinSW i rejestruje usługę `HomeSense` (konto wirtualne `NT SERVICE\HomeSense`, nie LocalSystem), instaluje AdGuard Home (pobranie + `AdGuardHome.exe -s install`), instaluje Grafanę (winget `GrafanaLabs.Grafana.OSS` lub zip) + wtyczkę `frser-sqlite-datasource`, kopiuje provisioning, reguła firewall dla 8080 i 53 (tylko profil Private), skrót tray w Autostarcie.
- `scripts/dev.ps1`: venv + `homesense serve --simulated` na laptopie dev.
- `grafana/provisioning/{datasources,dashboards}/*.yaml` + `grafana/dashboards/*.json` (Obecność, Urządzenia, DNS, Ruch).

## 9. Obsługa błędów
- Kolektory izolowane (6.1). Router niedostępny / złe hasło → status `down` dla kolektora, alert `collector_down` po 10 min (raz).
- DB: WAL, `busy_timeout=5000`, zapisy przez jeden writer (kolejka asyncio) by uniknąć `database is locked`.
- AdGuard niedostępny → UI sieci pokazuje „AdGuard niedostępny”, reszta działa.
- Brak profilu (za mało danych) → UI „zbieram dane (n/7 dni)”, detektory profilowe milczą.
- Konfiguracja walidowana przy starcie (`homesense doctor` sprawdza: router, AdGuard, Grafana, DB, porty).

## 10. Rozszerzalność (punkty rozszerzeń)
1. **Nowy kolektor** — klasa implementująca `Collector`, rejestracja w `registry.collectors` (też przez entry point `homesense.collectors`). Przyszłe: `esp32_csi` (obecność per strefa/pokój, ruch), `mmwave` (oddech, tętno) → nowe typy obserwacji `ZoneObservation`, `VitalsObservation` + nowe tabele w migracji.
2. **Nowy detektor** — klasa `Detector` w `patterns/detectors/` + rejestracja.
3. **Nowy kanał powiadomień** — klasa `Notifier` + rejestracja + pola w ustawieniach.
4. **Nowy router** — kolejny kolektor (np. `router_asus`, `router_openwrt`), wybór w configu `HOMESENSE_ROUTER_KIND`.
5. **Nowa strona UI** — router w `api/pages.py` + szablon.
6. **Migracje** — kolejny plik `storage/migrations/NNNN_opis.sql`.

## 11. Testowanie
- Jednostkowe: presence engine (histereza, restart), profile, każdy detektor, reguły alertów (cisza nocna, dedup), parser `arp -a`, mapowanie AdGuard querylog (fixture JSON), router collector (fake klient), ingest (nowe urządzenie, prywatność).
- Integracyjne: `simulated` 14 dni z `FakeClock` → profile niepuste, wykrycie wstrzykniętej anomalii; API przez `TestClient` (logowanie, CRUD osób/urządzeń, SSE, proxy Grafany z mockiem).
- Żaden test nie dotyka prawdziwej sieci; `respx` dla httpx.
- Komenda: `pytest -q`, lint: `ruff check .`.

## 12. Prywatność i zgodność
- Tylko metadane; brak przechwytywania pakietów/treści.
- `privacy_mode='stats_only'` per osoba: brak nazw domen w bazie dla jej urządzeń.
- Retencja 30 dni (twarde usuwanie).
- Strona „Co zbiera system”.
- Sekrety (hasło routera, token Telegram, hasło AdGuard) tylko w `.env` na komputerze docelowym, nigdy w repo.

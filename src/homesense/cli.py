"""Command line: serve, init-db, create-user, simulate, doctor, tray."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from collections import Counter
from datetime import timedelta

import httpx

from homesense.app import HomeSenseApp
from homesense.core.clock import FakeClock, SystemClock
from homesense.core.config import Settings
from homesense.core.models import AlertRaised
from homesense.patterns.detectors import run_detectors
from homesense.security import check_secret_key, hash_password
from homesense.storage.auth import migrate_databases, open_settings_auth_db
from homesense.storage.db import Database
from homesense.storage.repositories import UserRepo

log = logging.getLogger("homesense")

OK, FAIL, SKIP = "OK", "FAIL", "SKIP"
CHECK_TIMEOUT_S = 5
GUEST_BEFORE_END = timedelta(hours=3)
GUEST_MINUTES = 60


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs request URLs at INFO, and the Telegram bot token is part of the URL.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _settings() -> Settings:
    return Settings()


# --- serve ---


def _run_server(settings: Settings) -> None:
    import uvicorn

    from homesense.api.server import create_app

    # proxy_headers=False: X-Forwarded-For must never make a LAN client look like loopback
    # (/api/health gives loopback callers more detail).
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, proxy_headers=False)


def cmd_serve(args: argparse.Namespace) -> int:
    update: dict = {}
    if args.simulated:
        update["mode"] = "simulated"
    if args.host is not None:
        update["host"] = args.host
    if args.port is not None:
        update["port"] = args.port
    settings = _settings().model_copy(update=update)
    problems = check_secret_key(settings)
    if problems and settings.mode == "live":
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print("Refusing to start in live mode without a proper secret key.", file=sys.stderr)
        return 2
    for problem in problems:
        log.warning("!!! INSECURE (simulated mode only): %s !!!", problem)
    _run_server(settings)
    return 0


# --- init-db / create-user ---


def _open_databases(settings: Settings) -> tuple[Database, Database]:
    return Database(settings.db_path), open_settings_auth_db(settings)


def cmd_init_db(args: argparse.Namespace) -> int:
    db, auth_db = _open_databases(_settings())
    try:
        print(f"schema version {migrate_databases(db, auth_db)}")
    finally:
        db.close()
        auth_db.close()
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    password = args.password
    if password is None:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            print("error: passwords do not match", file=sys.stderr)
            return 1
    if not password:
        print("error: password must not be empty", file=sys.stderr)
        return 1
    db, auth_db = _open_databases(_settings())
    try:
        migrate_databases(db, auth_db)
        users = UserRepo(auth_db)
        if users.get_by_username(args.username) is None:
            users.create(args.username, hash_password(password))
            print(f"user {args.username} created")
        else:
            users.set_password(args.username, hash_password(password))
            print(f"password of user {args.username} updated")
    finally:
        db.close()
        auth_db.close()
    return 0


# --- simulate ---


def _ensure_sim_persons(app: HomeSenseApp) -> None:
    """Create persons for the simulated personas and assign their devices (phones count for presence)."""
    assert app.household is not None
    repos, now = app.repos, app.clock.now()
    existing = {p.name: p.id for p in repos.persons.list()}
    for persona in app.household.personas:
        person_id = existing.get(persona.name)
        if person_id is None:
            person_id = repos.persons.create(persona.name)
        for device in persona.devices:
            current = repos.devices.get(device.mac)
            if current is None:
                repos.devices.upsert_seen(device.mac, now, device.ip, device.hostname)
            elif current.person_id is not None:
                continue
            repos.devices.update(
                device.mac, person_id=person_id, kind=device.kind, counts_for_presence=device.kind == "phone"
            )


async def _simulate(app: HomeSenseApp, clock: FakeClock, days: int, step_s: int) -> Counter[str]:
    end = clock.now() + timedelta(days=days)
    alert_kinds: Counter[str] = Counter()

    async def count_alert(event: AlertRaised) -> None:
        alert_kinds[event.alert.kind] += 1

    app.initialize()
    app.bus.subscribe(AlertRaised, count_alert)
    for collector in app.collectors:
        collector.interval_s = step_s  # DNS queries are generated for the whole step window
    _ensure_sim_persons(app)
    assert app.household is not None
    app.household.inject_guest(end - GUEST_BEFORE_END, minutes=GUEST_MINUTES)

    step = timedelta(seconds=step_s)
    while True:
        await app.step()
        if clock.now() + step > end:
            break
        clock.advance(step)
    app.profiles.rebuild_all()
    await run_detectors(app.detectors, app.detector_context(), app.alerts)
    return alert_kinds


def cmd_simulate(args: argparse.Namespace) -> int:
    if args.days <= 0 or args.step_s <= 0:
        print("error: --days and --step-s must be positive", file=sys.stderr)
        return 1
    # History generation: simulated data only, and no external notifications for the replayed alerts.
    settings = _settings().model_copy(
        update={
            "mode": "simulated",
            "windows_toast": False,
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "ntfy_topic": "",
        }
    )
    clock = FakeClock(SystemClock().now() - timedelta(days=args.days))
    app = HomeSenseApp(settings, clock)

    async def run() -> Counter[str]:
        try:
            kinds = await _simulate(app, clock, args.days, args.step_s)
            print(f"simulated {args.days} days with {args.step_s} s steps into {settings.db_path}")
            print(f"persons: {len(app.repos.persons.list())}, devices: {len(app.repos.devices.list())}")
            return kinds
        finally:
            await app.stop()

    kinds = asyncio.run(run())
    print("alerts: " + (", ".join(f"{k}={n}" for k, n in sorted(kinds.items())) or "none"))
    return 0


# --- doctor ---


def _router_login(settings: Settings) -> None:
    from tplinkrouterc6u import TplinkRouterProvider

    client = TplinkRouterProvider.get_client(
        f"http://{settings.router_host}", settings.router_password, settings.router_username
    )
    client.authorize()
    client.logout()


async def _http_check(http: httpx.AsyncClient, url: str, auth: httpx.BasicAuth | None = None) -> tuple:
    try:
        response = await http.get(url, auth=auth, timeout=CHECK_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return FAIL, type(exc).__name__
    return (OK if response.is_success else FAIL), f"HTTP {response.status_code}"


async def run_checks(settings: Settings, http: httpx.AsyncClient) -> list[tuple[str, str, str]]:
    """(name, OK|FAIL|SKIP, detail) for the DB, router, AdGuard and Grafana."""
    results: list[tuple[str, str, str]] = []
    try:
        db, auth_db = _open_databases(settings)
        try:
            results.append(("database", OK, f"schema version {migrate_databases(db, auth_db)}"))
        finally:
            db.close()
            auth_db.close()
    except Exception as exc:
        results.append(("database", FAIL, f"{type(exc).__name__}: {exc}"))

    live = settings.mode == "live"
    if not live:
        results.append(("router", SKIP, "simulated mode"))
    elif not settings.router_enabled:
        results.append(("router", SKIP, "disabled"))
    else:
        try:
            await asyncio.to_thread(_router_login, settings)
            results.append(("router", OK, f"login to {settings.router_host}"))
        except Exception as exc:
            results.append(("router", FAIL, type(exc).__name__))

    if not live:
        results.append(("adguard", SKIP, "simulated mode"))
    elif not settings.adguard_enabled:
        results.append(("adguard", SKIP, "disabled"))
    else:
        auth = (
            httpx.BasicAuth(settings.adguard_username, settings.adguard_password)
            if settings.adguard_username
            else None
        )
        url = settings.adguard_url.rstrip("/") + "/control/status"
        results.append(("adguard", *await _http_check(http, url, auth)))

    results.append(("grafana", *await _http_check(http, settings.grafana_url.rstrip("/") + "/api/health")))
    return results


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings()

    async def run() -> list[tuple[str, str, str]]:
        async with httpx.AsyncClient() as http:
            return await run_checks(settings, http)

    results = asyncio.run(run())
    for name, status, detail in results:
        print(f"{name:<10} {status:<5} {detail}")
    return 1 if any(status == FAIL for _, status, _ in results) else 0


# --- tray ---


def cmd_tray(args: argparse.Namespace) -> int:
    from homesense.tray.app import main as tray_main

    tray_main(_settings())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="homesense", description="HomeSense home presence monitor")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web panel and collectors")
    serve.add_argument("--simulated", action="store_true", help="use the simulated household")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("init-db", help="apply database migrations").set_defaults(func=cmd_init_db)

    user = sub.add_parser("create-user", help="create a panel user or change its password")
    user.add_argument("username")
    user.add_argument("--password", help="prompted for when omitted")
    user.set_defaults(func=cmd_create_user)

    sim = sub.add_parser("simulate", help="generate simulated history (accelerated time)")
    sim.add_argument("--days", type=int, required=True)
    sim.add_argument("--step-s", type=int, default=60)
    sim.set_defaults(func=cmd_simulate)

    sub.add_parser("doctor", help="check DB, router, AdGuard and Grafana").set_defaults(func=cmd_doctor)
    sub.add_parser("tray", help="system tray icon and window").set_defaults(func=cmd_tray)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

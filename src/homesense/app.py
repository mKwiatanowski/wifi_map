"""Composition root: builds every component from Settings and runs the background jobs."""

from __future__ import annotations

import asyncio
import copy
import logging
import sys
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

import httpx

from homesense.alerts.dispatcher import NotificationDispatcher
from homesense.alerts.rules import DEFAULT_RULES, AlertRules
from homesense.alerts.service import AlertService
from homesense.collectors.adguard_dns import AdGuardDnsCollector
from homesense.collectors.arp_scan import ArpScanCollector
from homesense.collectors.base import Collector, CollectorRunner
from homesense.collectors.router_tplink import TplinkRouterCollector
from homesense.collectors.simulated import SimulatedCollector, SimulatedHousehold
from homesense.core.clock import Clock, SystemClock
from homesense.core.config import Settings
from homesense.core.events import EventBus
from homesense.core.models import AlertRaised, ObservationsBatch
from homesense.core.registry import Registry
from homesense.ingest import Ingest
from homesense.monitoring import MonitoringControl
from homesense.network.adguard import AdGuardClient
from homesense.notifications.base import Notifier
from homesense.notifications.log import LogNotifier
from homesense.notifications.ntfy import NtfyNotifier
from homesense.notifications.telegram import TelegramNotifier
from homesense.notifications.windows_toast import WindowsToastNotifier
from homesense.patterns.detectors import Detector, DetectorContext, default_detectors, run_detectors
from homesense.patterns.profiles import ProfileBuilder
from homesense.presence import coverage
from homesense.presence.engine import PresenceEngine
from homesense.scheduler import Periodic
from homesense.storage import retention
from homesense.storage.auth import migrate_databases, open_settings_auth_db
from homesense.storage.db import Database
from homesense.storage.repositories import Repos, UserRepo
from homesense.vendor import VendorLookup

log = logging.getLogger(__name__)

T = TypeVar("T")

PRESENCE_TICK_S = 30
PROFILES_REBUILD_S = 3600
# UnusualArrival looks back 10 minutes, so detectors must run at least that often.
DETECTORS_S = 300
RETENTION_S = 86400
VACUUM_S = 604800


class HomeSenseApp:
    def __init__(
        self, settings: Settings, clock: Clock | None = None, http: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self.clock: Clock = clock or SystemClock()
        self._http = http
        self._owns_http = False
        self.tasks: list[asyncio.Task] = []
        self._thread_jobs: set[asyncio.Future] = set()
        self._initialized = False

        self.db = Database(settings.db_path)
        self.repos = Repos.create(self.db, self.clock)
        self.auth_db = open_settings_auth_db(settings)
        self.users = UserRepo(self.auth_db, self.clock)
        self.bus = EventBus()
        self.monitoring = MonitoringControl(self.repos.monitoring, self.bus, self.clock)
        self.alerts = AlertService(self.repos, self.bus, self.clock, suppressed=self.monitoring.is_paused)
        self.vendor = VendorLookup()
        self.ingest = Ingest(self.repos, self.bus, self.alerts, self.clock, vendor=self.vendor.lookup)
        self.presence = PresenceEngine(
            self.repos,
            self.bus,
            self.alerts,
            self.clock,
            departure_timeout=timedelta(minutes=settings.departure_timeout_min),
            resumed_at=self.monitoring.resumed_at,
        )
        self.profiles = ProfileBuilder(self.repos, self.clock, tz=settings.timezone)
        self.detectors: list[Detector] = default_detectors()

        defaults = copy.deepcopy(DEFAULT_RULES)
        defaults["quiet_hours"].update(start=settings.quiet_hours_start, end=settings.quiet_hours_end)
        self.rules = AlertRules(self.repos.settings, defaults, settings.timezone)
        self.notifiers: Registry[Notifier] = self._build_notifiers()
        self.dispatcher = NotificationDispatcher(self.rules, self.notifiers, self.clock)

        simulated = settings.mode == "simulated"
        self.household: SimulatedHousehold | None = (
            SimulatedHousehold(
                self.clock,
                tz=settings.timezone,
                seed=settings.sim_seed,
                p0_dbm=settings.radar_p0_dbm,
                path_loss_n=settings.radar_path_loss_n,
            )
            if simulated
            else None
        )
        self.adguard: AdGuardClient | None = (
            AdGuardClient(
                settings.adguard_url,
                settings.adguard_username,
                settings.adguard_password,
                http=self._http_client(),
            )
            if not simulated and settings.adguard_enabled
            else None
        )
        self.collectors: list[Collector] = self.build_collectors()
        self.runner = CollectorRunner(
            self.collectors,
            self.ingest.handle,
            self.repos.collector_status,
            self.clock,
            is_paused=self.monitoring.is_paused,
        )

    # --- composition ---

    def _http_client(self) -> httpx.AsyncClient:
        """Shared HTTP client, created on first use when none was injected."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10)
            self._owns_http = True
        return self._http

    @property
    def http(self) -> httpx.AsyncClient:
        """The shared HTTP client (e.g. for the Grafana proxy); closed by stop() when owned."""
        return self._http_client()

    def _build_notifiers(self) -> Registry[Notifier]:
        s = self.settings
        notifiers: Registry[Notifier] = Registry()
        notifiers.register("log", LogNotifier())
        if s.telegram_bot_token and s.telegram_chat_id:
            notifiers.register(
                "telegram",
                TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id, http=self._http_client()),
            )
        if s.ntfy_topic:
            notifiers.register("ntfy", NtfyNotifier(s.ntfy_url, s.ntfy_topic, http=self._http_client()))
        if s.windows_toast and sys.platform == "win32":
            notifiers.register("windows", WindowsToastNotifier())
        return notifiers

    def build_collectors(self) -> list[Collector]:
        s = self.settings
        if s.mode == "simulated":
            assert self.household is not None
            return [
                SimulatedCollector(
                    self.household, survey=lambda: coverage.active_targets(self.repos, self.clock.now())
                )
            ]
        collectors: list[Collector] = []
        if s.router_enabled:
            collectors.append(
                TplinkRouterCollector(
                    s.router_host, s.router_username, s.router_password, self.clock, s.router_interval_s
                )
            )
        if s.arp_enabled:
            collectors.append(ArpScanCollector(s.arp_subnet, self.clock, s.arp_interval_s))
        if s.adguard_enabled and self.adguard is not None:
            collectors.append(
                AdGuardDnsCollector(
                    self.adguard,
                    self.repos.settings,
                    self.clock,
                    s.adguard_interval_s,
                    not_before=self.monitoring.resumed_at,
                )
            )
        return collectors

    def detector_context(self) -> DetectorContext:
        return DetectorContext(
            repos=self.repos, now=self.clock.now(), tz=self.settings.timezone, presence=self.presence.states()
        )

    # --- lifecycle ---

    def initialize(self) -> None:
        """Migrate the DBs, restore presence state and wire bus subscriptions (idempotent)."""
        if self._initialized:
            return
        migrate_databases(self.db, self.auth_db)
        self.monitoring.load()
        self.presence.restore()
        self.bus.subscribe(ObservationsBatch, self.presence.on_batch)
        self.bus.subscribe(AlertRaised, self.dispatcher.on_alert)
        self._initialized = True

    async def start(self) -> None:
        self.initialize()
        jobs = [
            Periodic("presence", PRESENCE_TICK_S, self._presence_tick),
            Periodic("profiles", PROFILES_REBUILD_S, self._rebuild_profiles),
            Periodic("detectors", DETECTORS_S, self._run_detectors),
            Periodic("retention", RETENTION_S, self._purge_old),
            Periodic("vacuum", VACUUM_S, self._vacuum, run_immediately=False),
        ]
        self.tasks = [asyncio.create_task(self.runner.run_forever(), name="collectors")]
        self.tasks += [asyncio.create_task(job.run_forever(), name=job.name) for job in jobs]

    async def stop(self) -> None:
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A cancelled job's worker thread keeps running; let it finish before the DB is closed.
        await asyncio.gather(*self._thread_jobs, return_exceptions=True)
        if self.adguard is not None:
            await self.adguard.aclose()
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
            self._owns_http = False
        self.db.close()
        self.auth_db.close()

    async def step(self, collect: bool = True) -> None:
        """One synchronous cycle (simulate & tests): run every collector once, then a presence tick."""
        self.initialize()
        if collect:
            for collector in self.collectors:
                await self.runner.run_once(collector)
        await self._presence_tick()

    # --- periodic jobs ---

    async def _in_thread(self, fn: Callable[..., T], *args: Any) -> T:
        """Run blocking DB work off the event loop; stop() waits for it even when the job is cancelled."""
        future = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        self._thread_jobs.add(future)
        future.add_done_callback(self._thread_jobs.discard)
        return await asyncio.shield(future)

    async def _presence_tick(self) -> None:
        """Also applies the auto-resume of a timed monitoring pause; presence is frozen while paused."""
        await self.monitoring.expire()
        if self.monitoring.is_paused():
            return
        await self.presence.tick()

    async def _rebuild_profiles(self) -> None:
        if self.monitoring.is_paused():
            return
        await self._in_thread(self.profiles.rebuild_all)

    async def _run_detectors(self) -> None:
        if self.monitoring.is_paused():
            return
        await run_detectors(self.detectors, self.detector_context(), self.alerts)

    async def _purge_old(self) -> None:
        deleted = await self._in_thread(
            retention.purge_old, self.repos, self.clock.now(), self.settings.retention_days
        )
        log.info("retention purge removed %s", deleted)

    async def _vacuum(self) -> None:
        await self._in_thread(retention.vacuum, self.db)

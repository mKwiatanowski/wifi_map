from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from homesense.alerts.service import AlertService
from homesense.core.clock import Clock
from homesense.core.events import EventBus
from homesense.core.models import Alert, Device, ObservationsBatch, Person, PresenceEvent
from homesense.storage.repositories import Repos


@dataclass
class PersonState:
    person_id: int
    home: bool
    since: datetime | None
    via_mac: str | None


class PresenceEngine:
    """Decides who is home from the last_seen of their presence devices.

    A person stays home until none of their presence devices has been seen for longer than
    `departure_timeout` (hysteresis against phones dropping off WiFi while asleep). The leave
    is then back-dated to the last sighting.
    """

    def __init__(
        self, repos: Repos, bus: EventBus, alerts: AlertService, clock: Clock, departure_timeout: timedelta
    ) -> None:
        self._repos = repos
        self._bus = bus
        self._alerts = alerts
        self._clock = clock
        self._timeout = departure_timeout
        self._states: dict[int, PersonState] = {}
        self._restored = False
        self._lock = asyncio.Lock()

    def restore(self) -> None:
        """Rebuild state from storage: an open interval means home since its start."""
        self._states = {}
        for person in self._repos.persons.list():
            interval = self._repos.presence.open_interval(person.id)
            if interval is not None:
                self._states[person.id] = PersonState(person.id, True, interval.start_ts, None)
            else:
                self._states[person.id] = PersonState(person.id, False, None, None)
        self._restored = True

    async def tick(self) -> list[PresenceEvent]:
        async with self._lock:
            if not self._restored:
                self.restore()
            now = self._clock.now()
            events: list[PresenceEvent] = []
            persons = self._repos.persons.list()
            self._states = {
                p.id: self._states.get(p.id, PersonState(p.id, False, None, None)) for p in persons
            }
            for person in persons:
                event = await self._evaluate(person, now)
                if event is not None:
                    events.append(event)
            return events

    async def on_batch(self, batch: ObservationsBatch) -> None:
        await self.tick()

    def states(self) -> list[PersonState]:
        return list(self._states.values())

    async def _evaluate(self, person: Person, now: datetime) -> PresenceEvent | None:
        state = self._states[person.id]
        latest = self._latest_presence_device(person.id)
        home_now = latest is not None and now - latest.last_seen <= self._timeout

        if home_now and not state.home:
            event = PresenceEvent(ts=latest.last_seen, person_id=person.id, kind="arrive", mac=latest.mac)
            self._repos.presence.start_interval(person.id, event.ts)
            self._states[person.id] = PersonState(person.id, True, event.ts, latest.mac)
        elif not home_now and state.home:
            mac = latest.mac if latest is not None else state.via_mac
            ts = latest.last_seen if latest is not None else now
            event = PresenceEvent(ts=ts, person_id=person.id, kind="leave", mac=mac)
            self._repos.presence.close_interval(person.id, event.ts)
            self._states[person.id] = PersonState(person.id, False, event.ts, None)
        else:
            if home_now:
                state.via_mac = latest.mac
            return None

        self._repos.presence.add_event(event)
        await self._bus.publish(event)
        await self._alerts.raise_alert(self._alert_for(person, event))
        return event

    def _latest_presence_device(self, person_id: int) -> Device | None:
        devices = [d for d in self._repos.devices.for_person(person_id) if d.counts_for_presence]
        return max(devices, key=lambda d: d.last_seen, default=None)

    @staticmethod
    def _alert_for(person: Person, event: PresenceEvent) -> Alert:
        title = (
            f"{person.name} wrócił(a) do domu" if event.kind == "arrive" else f"{person.name} wyszedł/wyszła"
        )
        return Alert(
            kind=event.kind,
            severity="info",
            title=title,
            message=title,
            ts=event.ts,
            person_id=person.id,
            mac=event.mac,
            dedup_key=None,
        )

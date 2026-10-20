from __future__ import annotations

from dataclasses import dataclass

from homesense.core.clock import Clock
from homesense.storage.db import Database
from homesense.storage.repositories.alerts import AlertRepo
from homesense.storage.repositories.collector_status import CollectorStatusRepo
from homesense.storage.repositories.devices import DeviceRepo
from homesense.storage.repositories.dns import DnsRepo
from homesense.storage.repositories.observations import ObservationRepo
from homesense.storage.repositories.persons import PersonRepo
from homesense.storage.repositories.presence import PresenceRepo
from homesense.storage.repositories.profiles import ProfileRepo
from homesense.storage.repositories.settings import SettingsRepo
from homesense.storage.repositories.users import UserRepo

__all__ = [
    "AlertRepo",
    "CollectorStatusRepo",
    "DeviceRepo",
    "DnsRepo",
    "ObservationRepo",
    "PersonRepo",
    "PresenceRepo",
    "ProfileRepo",
    "Repos",
    "SettingsRepo",
    "UserRepo",
]


@dataclass
class Repos:
    persons: PersonRepo
    devices: DeviceRepo
    observations: ObservationRepo
    presence: PresenceRepo
    dns: DnsRepo
    alerts: AlertRepo
    profiles: ProfileRepo
    settings: SettingsRepo
    collector_status: CollectorStatusRepo

    @classmethod
    def create(cls, db: Database, clock: Clock | None = None) -> Repos:
        """Repositories over homesense.db; `clock` stamps created_at, defaults to SystemClock.

        Login accounts are not here: `UserRepo` works on the separate auth DB (storage.auth).
        """
        return cls(
            persons=PersonRepo(db, clock),
            devices=DeviceRepo(db, clock),
            observations=ObservationRepo(db, clock),
            presence=PresenceRepo(db, clock),
            dns=DnsRepo(db, clock),
            alerts=AlertRepo(db, clock),
            profiles=ProfileRepo(db, clock),
            settings=SettingsRepo(db, clock),
            collector_status=CollectorStatusRepo(db, clock),
        )

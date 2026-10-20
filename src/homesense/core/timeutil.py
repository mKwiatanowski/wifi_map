from datetime import UTC, datetime
from zoneinfo import ZoneInfo

__all__ = ["UTC", "floor_hour", "require_aware", "to_local", "ts_from_db", "ts_to_db"]

_FMT = "%Y-%m-%d %H:%M:%S"


def require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime not allowed")
    return dt


def ts_to_db(dt: datetime) -> str:
    require_aware(dt)
    return dt.astimezone(UTC).strftime(_FMT)


def ts_from_db(s: str) -> datetime:
    return datetime.strptime(s, _FMT).replace(tzinfo=UTC)


def to_local(dt: datetime, tz: str) -> datetime:
    return require_aware(dt).astimezone(ZoneInfo(tz))


def floor_hour(dt: datetime) -> datetime:
    return require_aware(dt).astimezone(UTC).replace(minute=0, second=0, microsecond=0)

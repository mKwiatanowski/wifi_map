from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from homesense.core.models import CoveragePlan, CoveragePoint, CoverageRoom, normalize_mac
from homesense.core.timeutil import ts_from_db, ts_to_db
from homesense.storage.repositories._base import Repo

_POINT_COLS = "id, mac, x, y, started_at, ends_at, done, rssi, samples, band"


def _point(r) -> CoveragePoint:
    return CoveragePoint(
        id=r.id,
        mac=r.mac,
        x=r.x,
        y=r.y,
        started_at=ts_from_db(r.started_at),
        ends_at=ts_from_db(r.ends_at),
        done=bool(r.done),
        rssi=r.rssi,
        samples=r.samples,
        band=r.band,
    )


class CoverageRepo(Repo):
    """Coverage survey (plan, rooms, measurement points). Never purged by retention."""

    def get_plan(self) -> CoveragePlan | None:
        with self.db.connect() as c:
            p = c.execute(text("SELECT width_m, height_m, router_x, router_y FROM coverage_plan")).first()
            if p is None:
                return None
            rooms = c.execute(text("SELECT name, x, y, w, h FROM coverage_rooms ORDER BY id"))
            router = None if p.router_x is None or p.router_y is None else (p.router_x, p.router_y)
            return CoveragePlan(
                p.width_m, p.height_m, router, tuple(CoverageRoom(r.name, r.x, r.y, r.w, r.h) for r in rooms)
            )

    def save_plan(self, plan: CoveragePlan) -> None:
        """Replaces the plan and all its rooms."""
        rx, ry = plan.router if plan.router is not None else (None, None)
        with self.db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO coverage_plan(id, width_m, height_m, router_x, router_y, updated_at) "
                    "VALUES (1, :w, :h, :rx, :ry, :t) ON CONFLICT(id) DO UPDATE SET "
                    "width_m = excluded.width_m, height_m = excluded.height_m, router_x = excluded.router_x, "
                    "router_y = excluded.router_y, updated_at = excluded.updated_at"
                ),
                {"w": plan.width_m, "h": plan.height_m, "rx": rx, "ry": ry, "t": ts_to_db(self.clock.now())},
            )
            c.execute(text("DELETE FROM coverage_rooms"))
            if plan.rooms:
                c.execute(
                    text("INSERT INTO coverage_rooms(name, x, y, w, h) VALUES (:n, :x, :y, :w, :h)"),
                    [{"n": r.name, "x": r.x, "y": r.y, "w": r.w, "h": r.h} for r in plan.rooms],
                )

    def add_point(
        self, mac: str, x: float, y: float, started_at: datetime, ends_at: datetime
    ) -> CoveragePoint:
        with self.db.begin() as c:
            point_id = c.execute(
                text(
                    "INSERT INTO coverage_points(mac, x, y, started_at, ends_at) VALUES (:m, :x, :y, :s, :e)"
                ),
                {"m": normalize_mac(mac), "x": x, "y": y, "s": ts_to_db(started_at), "e": ts_to_db(ends_at)},
            ).lastrowid
        point = self.get_point(point_id)
        assert point is not None
        return point

    def get_point(self, point_id: int) -> CoveragePoint | None:
        with self.db.connect() as c:
            r = c.execute(
                text(f"SELECT {_POINT_COLS} FROM coverage_points WHERE id = :i"), {"i": point_id}
            ).first()
        return _point(r) if r else None

    def points(self) -> list[CoveragePoint]:
        with self.db.connect() as c:
            return [
                _point(r) for r in c.execute(text(f"SELECT {_POINT_COLS} FROM coverage_points ORDER BY id"))
            ]

    def measuring(self) -> list[CoveragePoint]:
        """Points whose measurement has not been finished yet."""
        with self.db.connect() as c:
            rows = c.execute(text(f"SELECT {_POINT_COLS} FROM coverage_points WHERE done = 0 ORDER BY id"))
            return [_point(r) for r in rows]

    def finish_point(self, point_id: int, rssi: float | None, samples: int, band: str) -> None:
        with self.db.begin() as c:
            c.execute(
                text("UPDATE coverage_points SET done = 1, rssi = :r, samples = :n, band = :b WHERE id = :i"),
                {"r": rssi, "n": samples, "b": band, "i": point_id},
            )

    def delete_point(self, point_id: int) -> bool:
        with self.db.begin() as c:
            return c.execute(text("DELETE FROM coverage_points WHERE id = :i"), {"i": point_id}).rowcount > 0

    def clear_points(self) -> int:
        with self.db.begin() as c:
            return c.execute(text("DELETE FROM coverage_points")).rowcount

from __future__ import annotations

from sqlalchemy import text

from homesense.core.timeutil import ts_to_db
from homesense.storage.repositories._base import Repo


class UserRepo(Repo):
    """Login accounts; `db` is the auth database (storage.auth.open_auth_db), not homesense.db."""

    def create(self, username: str, password_hash: str) -> int:
        with self.db.begin() as c:
            return c.execute(
                text("INSERT INTO users(username, password_hash, created_at) VALUES (:u, :h, :t)"),
                {"u": username, "h": password_hash, "t": ts_to_db(self.clock.now())},
            ).lastrowid

    def create_first(self, username: str, password_hash: str) -> bool:
        """Create the user only while the table is empty (atomic); True if it was created."""
        with self.db.begin() as c:
            return (
                c.execute(
                    text(
                        "INSERT INTO users(username, password_hash, created_at) "
                        "SELECT :u, :h, :t WHERE NOT EXISTS (SELECT 1 FROM users)"
                    ),
                    {"u": username, "h": password_hash, "t": ts_to_db(self.clock.now())},
                ).rowcount
                == 1
            )

    def get_by_username(self, username: str) -> tuple[int, str, str] | None:
        """Returns (id, username, password_hash) or None."""
        with self.db.connect() as c:
            r = c.execute(
                text("SELECT id, username, password_hash FROM users WHERE username = :u"), {"u": username}
            ).first()
        return (r.id, r.username, r.password_hash) if r else None

    def delete(self, username: str) -> None:
        with self.db.begin() as c:
            c.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})

    def count(self) -> int:
        with self.db.connect() as c:
            return c.execute(text("SELECT COUNT(*) FROM users")).scalar_one()

    def set_password(self, username: str, password_hash: str) -> None:
        with self.db.begin() as c:
            c.execute(
                text("UPDATE users SET password_hash = :h WHERE username = :u"),
                {"u": username, "h": password_hash},
            )

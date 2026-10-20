from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOMESENSE_", env_file=".env", extra="ignore")

    mode: Literal["live", "simulated"] = "live"
    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 8080
    timezone: str = "Europe/Warsaw"
    secret_key: str = "change-me"

    router_enabled: bool = True
    router_host: str = "192.168.0.1"
    router_username: str = "admin"
    router_password: str = ""
    router_interval_s: int = 60

    arp_enabled: bool = True
    arp_subnet: str = "192.168.0.0/24"
    arp_interval_s: int = 120

    adguard_enabled: bool = True
    adguard_url: str = "http://127.0.0.1:3001"
    adguard_username: str = ""
    adguard_password: str = ""
    adguard_interval_s: int = 30

    grafana_url: str = "http://127.0.0.1:3000"

    departure_timeout_min: int = 15
    # Device radar (Mapa): log-distance path loss, RSSI at 1 m and the path loss exponent.
    radar_p0_dbm: float = -40.0
    radar_path_loss_n: float = 3.0
    retention_days: int = 30

    windows_toast: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""

    quiet_hours_start: int = 23
    quiet_hours_end: int = 7
    sim_seed: int = 42

    @field_validator("data_dir")
    @classmethod
    def _absolute_data_dir(cls, value: Path) -> Path:
        """Resolved once, so DB paths never depend on whatever the CWD is later."""
        return Path(value).expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "homesense.db"

    @property
    def auth_dir(self) -> Path:
        """Private folder for login accounts and the setup token.

        Grafana needs write access to data_dir (SQLite WAL of homesense.db); the Windows installer
        puts an inheritable deny for Grafana's account on this folder, so files created here later
        (auth.db-wal, auth.db-shm, setup-token.txt) are never readable by it.
        """
        return self.data_dir / "auth"

    @property
    def auth_db_path(self) -> Path:
        """Login accounts; separate from db_path because Grafana reads homesense.db."""
        return self.auth_dir / "auth.db"

    @property
    def setup_token_path(self) -> Path:
        return self.auth_dir / "setup-token.txt"

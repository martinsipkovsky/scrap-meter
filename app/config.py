"""Application configuration, read from environment variables."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # postgresql+psycopg://user:pass@host:5432/db  (defaults to a local sqlite
    # file so the app can run without Postgres for development / tests)
    database_url: str = "sqlite+pysqlite:///./cognex.db"

    secret_key: str = "change-me-in-production"

    default_admin_user: str = "Admin"
    default_admin_password: str = "1234"

    # Background poller
    poll_enabled: bool = True
    # Safety floor: no device is polled faster than this many seconds
    min_poll_interval: int = 1

    # Ports the TCP listener (cameras pushing data) may use. Must match the
    # range docker-compose publishes, otherwise cameras cannot reach them.
    listen_ports: str = "5100-5119"

    # Persistent app files (the database chosen on the Database page). Mount a
    # volume here in docker so the setting survives container updates.
    data_dir: str = "./data"

    # Start the linked WhatsApp client (if a phone was linked) with the app.
    whatsapp_enabled: bool = True

    # Read-only database login for Power BI (Postgres only, see app.reporting).
    # Made or updated on startup when a password is set; it may only read the
    # powerbi_* views.
    powerbi_user: str = "powerbi"
    powerbi_password: str = ""
    # the port the database is published on for them (docker-compose
    # override), shown on the Database page; 0 = not published
    powerbi_db_port: int = 0


settings = Settings()

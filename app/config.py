"""Twelve-Factor III: all config comes from environment variables."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings
from sqlalchemy import URL


class Settings(BaseSettings):
    port: int = 8000
    log_level: str = "INFO"
    shutdown_grace_seconds: int = 20

    # Backing service: Postgres (factor IV).
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "railbook"
    # Local runs and tests set the user/password directly...
    db_user: str = "railbook"
    db_password: SecretStr = SecretStr("")
    # ...in the cluster, Vault Agent renders them into this JSON file ({"username", "password"})
    # and replaces it when Vault rotates the user. Read on every new connection (app/db.py).
    db_credentials_file: str | None = None
    # Group role that owns the tables. Vault's short-lived users are members of it
    # and switch to it on connect, so tables never belong to a user that expires.
    db_role: str | None = None
    db_pool_size: int = 5

    # Mock payment provider key, from Vault KV.
    payment_api_key: SecretStr = SecretStr("")

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

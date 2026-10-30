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

    # Login (OIDC, Keycloak). Not secrets: the API only needs Keycloak's PUBLIC keys.
    # Defaults match the kind setup; they also keep the pre-upgrade setup Job working, which
    # runs with the previous release's ConfigMap.
    # "iss" in every token: the public URL the browser uses, not the in-cluster one.
    oidc_issuer: str = "http://auth.localhost/realms/railbook"
    # Where the public keys are fetched: Keycloak's Service inside the cluster.
    oidc_jwks_url: str = (
        "http://keycloak.keycloak.svc:8080/realms/railbook/protocol/openid-connect/certs"
    )
    # "aud" every token must contain: tokens issued for other apps are refused.
    oidc_audience: str = "railbook-api"
    # Realm role that may see and cancel every booking.
    oidc_admin_role: str = "railbook-admin"

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

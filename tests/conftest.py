import os

# Config comes from the environment (factor III); defaults suit a local test Postgres.
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5433")
os.environ.setdefault("DB_NAME", "railbook")
os.environ.setdefault("DB_USER", "railbook")
os.environ.setdefault("DB_PASSWORD", "railbook")
os.environ.setdefault("PAYMENT_API_KEY", "test-key")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import time  # noqa: E402

import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import auth  # noqa: E402
from app.admin import setup  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    engine = get_engine()
    Base.metadata.drop_all(engine)
    # Forget applied migrations too, so setup() runs them all again on the empty database.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    with Session(engine) as session:
        setup(session)
    yield


# Tests play Keycloak: their own RSA key pair signs the tokens. Only where the public key comes
# from is replaced (no HTTP to Keycloak); every check in app/auth.py:decode() runs for real.
KID = "test-key"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ALICE, BOB, ADMIN = "alice-sub", "bob-sub", "admin-sub"


class FakeJWKS:
    def get_signing_key_from_jwt(self, token):
        if jwt.get_unverified_header(token).get("kid") != KID:
            raise jwt.PyJWKClientError("Unable to find a signing key that matches")
        return jwt.PyJWK.from_dict(
            {
                **jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key(), as_dict=True),
                "kid": KID,
                "alg": "RS256",
                "use": "sig",
            }
        )


def make_token(sub=ALICE, roles=(), key=PRIVATE_KEY, kid=KID, **claims):
    settings = get_settings()
    now = int(time.time())
    payload = {
        "iss": settings.oidc_issuer,
        "aud": settings.oidc_audience,
        "sub": sub,
        "iat": now,
        "exp": now + 300,
        "preferred_username": sub,
        "realm_access": {"roles": list(roles)},
        **claims,
    }
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def fake_keycloak(monkeypatch):
    monkeypatch.setattr(auth, "jwks_client", FakeJWKS)


@pytest.fixture
def anon():
    """A client with no token."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(anon):
    """Logged in as alice (a regular user)."""
    anon.headers.update(bearer(make_token(ALICE)))
    return anon

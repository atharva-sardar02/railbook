import os

# Config comes from the environment (factor III); defaults suit a local test Postgres.
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5433")
os.environ.setdefault("DB_NAME", "railbook")
os.environ.setdefault("DB_USER", "railbook")
os.environ.setdefault("DB_PASSWORD", "railbook")
os.environ.setdefault("PAYMENT_API_KEY", "test-key")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.admin import setup  # noqa: E402
from app.db import get_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    engine = get_engine()
    Base.metadata.drop_all(engine)
    with Session(engine) as session:
        setup(session)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c

import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine
from app.models import Inventory

TODAY = date.today().isoformat()


def book(client, seats=1, class_code="3A", **extra):
    return client.post(
        "/bookings",
        json={
            "train_number": "12952",
            "travel_date": TODAY,
            "class_code": class_code,
            "passenger_name": "Asha",
            "seats": seats,
            **extra,
        },
    )


def seats_left(client, class_code="3A"):
    trains = client.get("/trains", params={"from": "NDLS", "to": "BCT", "date": TODAY}).json()
    return next(c["available"] for c in trains[0]["classes"] if c["class_code"] == class_code)


def test_health_and_readiness(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").json() == {"status": "ready"}


def test_stations_sorted_by_name(client):
    stations = client.get("/stations").json()
    assert len(stations) == 5
    assert [s["name"] for s in stations] == sorted(s["name"] for s in stations)


def test_frontend_is_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert '<script src="/static/app.js"' in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert '<script src="/static/auth.js"' in page.text
    assert client.get("/static/auth.js").status_code == 200


def test_auth_config_is_public(anon):
    assert anon.get("/auth-config").json() == {
        "issuer": "http://auth.localhost/realms/railbook",
        "client_id": "railbook-web",
    }


def test_search_returns_seeded_train(client):
    resp = client.get("/trains", params={"from": "ndls", "to": "bct", "date": TODAY})
    assert resp.status_code == 200
    trains = resp.json()
    assert [t["number"] for t in trains] == ["12952"]
    assert {c["class_code"] for c in trains[0]["classes"]} == {"SL", "3A", "2A"}


def test_book_lookup_and_cancel(client):
    before = seats_left(client)
    resp = book(client, seats=2)
    assert resp.status_code == 201
    pnr = resp.json()["pnr"]
    assert resp.json()["amount"] == "3500.00"
    assert seats_left(client) == before - 2

    assert client.get(f"/bookings/{pnr}").json()["status"] == "CONFIRMED"

    resp = client.delete(f"/bookings/{pnr}")
    assert resp.json()["status"] == "CANCELLED"
    assert seats_left(client) == before
    assert client.delete(f"/bookings/{pnr}").status_code == 409


def test_unknown_train_and_pnr(client):
    assert book(client, class_code="1A").status_code == 404
    assert client.get("/bookings/0000000000").status_code == 404


def test_sold_out(client):
    with Session(get_engine()) as s:
        s.execute(update(Inventory).values(available=1))
        s.commit()
    resp = book(client, seats=2)
    assert resp.status_code == 409


def test_payment_failure_keeps_seats(client):
    before = seats_left(client)
    assert book(client, simulate_payment_failure=True).status_code == 402
    assert seats_left(client) == before


def test_concurrent_bookings_never_oversell(client):
    with Session(get_engine()) as s:
        s.execute(update(Inventory).values(available=5))
        s.commit()
    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(lambda _: book(client).status_code, range(20)))
    assert codes.count(201) == 5
    assert codes.count(409) == 15
    assert seats_left(client) == 0


def test_request_id_is_echoed(client):
    resp = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["X-Request-ID"] == "abc-123"
    assert client.get("/healthz").headers["X-Request-ID"]


def test_metrics_exposed(client):
    book(client)
    body = client.get("/metrics").text
    assert 'railbook_bookings_total{status="confirmed"}' in body
    assert "http_request_duration_seconds" in body


def test_db_credentials_read_from_file_on_each_connect(tmp_path, monkeypatch):
    # In the cluster Vault Agent rewrites this file when it rotates the DB user; new connections
    # must use what is in the file now, not what was in the environment at startup.
    # The real test-DB login comes from the environment (conftest.py), like every other run.
    user = os.environ["DB_USER"]
    creds = tmp_path / "db.json"
    creds.write_text(json.dumps({"username": user, "password": os.environ["DB_PASSWORD"]}))
    monkeypatch.setenv("DB_PASSWORD", secrets.token_hex(16))  # must not be used
    monkeypatch.setenv("DB_CREDENTIALS_FILE", str(creds))
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        engine = get_engine()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_user")).scalar() == user

        # Simulate Vault rotating the user: the old password no longer works.
        creds.write_text(json.dumps({"username": user, "password": secrets.token_hex(16)}))
        engine.dispose()  # drop pooled connections so the next one is new
        try:
            engine.connect()
            raise AssertionError("connected with the old credentials")
        except OperationalError:
            pass
    finally:
        get_engine().dispose()
        get_settings.cache_clear()
        get_engine.cache_clear()

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import auth
from app.db import get_engine
from app.models import Booking
from tests.conftest import ADMIN, ALICE, BOB, PRIVATE_KEY, bearer, make_token
from tests.test_api import TODAY, book

ALICE_H, BOB_H = bearer(make_token(ALICE)), bearer(make_token(BOB))
ADMIN_H = bearer(make_token(ADMIN, roles=["railbook-admin"]))


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def hs256_signed_with_public_key() -> str:
    # The classic "algorithm confusion" forgery: the public key is public, so an attacker can
    # use it as an HMAC secret. Accepting the token's own "alg" would make this pass.
    pem = PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    good = jwt.decode(make_token(), options={"verify_signature": False})
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "test-key"}).encode())
    body = b64(json.dumps(good).encode())
    sig = hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64(sig)}"


def tampered() -> str:
    # A real alice token with the payload changed to bob: the signature no longer matches.
    head, _, sig = make_token(ALICE).split(".")
    claims = jwt.decode(make_token(ALICE), options={"verify_signature": False}) | {"sub": BOB}
    return f"{head}.{b64(json.dumps(claims).encode())}.{sig}"


BAD_TOKENS = {
    "not a JWT": lambda: "garbage",
    "expired": lambda: make_token(exp=int(time.time()) - 60),
    "other issuer": lambda: make_token(iss="http://evil.localhost/realms/railbook"),
    "other audience": lambda: make_token(aud="some-other-app"),
    "signed by another key": lambda: make_token(
        key=rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ),
    "unknown key id": lambda: make_token(kid="not-in-jwks"),
    "alg none": lambda: jwt.encode(
        jwt.decode(make_token(), options={"verify_signature": False}), None, algorithm="none",
        headers={"kid": "test-key"},  # a known kid, so it reaches the algorithm check
    ),
    "HS256 with the public key": hs256_signed_with_public_key,
    "payload changed": tampered,
    "no sub": lambda: jwt.encode(
        {k: v for k, v in jwt.decode(make_token(), options={"verify_signature": False}).items()
         if k != "sub"},
        PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"},
    ),
}


def test_search_needs_no_login(anon):
    assert anon.get("/stations").status_code == 200
    search = {"from": "NDLS", "to": "BCT", "date": TODAY}
    assert anon.get("/trains", params=search).status_code == 200
    assert anon.get("/").status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/bookings"), ("GET", "/bookings"), ("GET", "/bookings/0000000000"),
     ("DELETE", "/bookings/0000000000")],
)
def test_bookings_need_a_token(anon, method, path):
    resp = anon.request(method, path, json={} if method == "POST" else None)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Bearer")


@pytest.mark.parametrize("name", BAD_TOKENS)
def test_bad_tokens_are_rejected(anon, name):
    resp = anon.get("/bookings", headers=bearer(BAD_TOKENS[name]()))
    assert resp.status_code == 401, name


def test_owner_is_taken_from_the_token(anon):
    # A user_id in the body is ignored: the owner can only come from the signed token.
    resp = anon.post(
        "/bookings",
        headers=ALICE_H,
        json={"train_number": "12952", "travel_date": TODAY, "class_code": "3A",
              "passenger_name": "Asha", "seats": 1, "user_id": BOB},
    )
    assert resp.status_code == 201
    with Session(get_engine()) as s:
        assert s.scalar(select(Booking.user_id).where(Booking.pnr == resp.json()["pnr"])) == ALICE


def test_only_owner_or_admin_can_see_and_cancel(anon):
    pnr = anon.post(
        "/bookings", headers=ALICE_H,
        json={"train_number": "12952", "travel_date": TODAY, "class_code": "3A",
              "passenger_name": "Asha", "seats": 1},
    ).json()["pnr"]

    assert anon.get(f"/bookings/{pnr}", headers=BOB_H).status_code == 403
    assert anon.delete(f"/bookings/{pnr}", headers=BOB_H).status_code == 403
    assert anon.get(f"/bookings/{pnr}", headers=ALICE_H).json()["status"] == "CONFIRMED"

    assert anon.get(f"/bookings/{pnr}", headers=ADMIN_H).status_code == 200
    assert anon.delete(f"/bookings/{pnr}", headers=ADMIN_H).json()["status"] == "CANCELLED"


def test_list_shows_only_your_bookings(client, anon):
    alice_pnr = book(client).json()["pnr"]
    bob_pnr = anon.post(
        "/bookings", headers=BOB_H,
        json={"train_number": "12952", "travel_date": TODAY, "class_code": "SL",
              "passenger_name": "Bob", "seats": 1},
    ).json()["pnr"]

    assert [b["pnr"] for b in anon.get("/bookings", headers=ALICE_H).json()] == [alice_pnr]
    assert [b["pnr"] for b in anon.get("/bookings", headers=BOB_H).json()] == [bob_pnr]
    assert {b["pnr"] for b in anon.get("/bookings", headers=ADMIN_H).json()} == {
        alice_pnr, bob_pnr
    }


def test_bookings_from_before_login_are_admin_only(client, anon):
    pnr = book(client).json()["pnr"]
    with Session(get_engine()) as s:
        s.execute(update(Booking).where(Booking.pnr == pnr).values(user_id=None))
        s.commit()

    assert anon.get(f"/bookings/{pnr}", headers=ALICE_H).status_code == 403
    assert anon.get("/bookings", headers=ALICE_H).json() == []
    assert anon.get(f"/bookings/{pnr}", headers=ADMIN_H).status_code == 200


def test_keycloak_unreachable_is_503_not_401(anon, monkeypatch):
    class Down:
        def get_signing_key_from_jwt(self, token):
            raise jwt.PyJWKClientConnectionError("connection refused")

    monkeypatch.setattr(auth, "jwks_client", Down)
    assert anon.get("/bookings", headers=ALICE_H).status_code == 503

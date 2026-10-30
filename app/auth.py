"""Who is calling: checks the Keycloak access token (a JWT) in the Authorization header.

The API never calls Keycloak per request. It checks the token's signature with Keycloak's
public keys (JWKS, fetched once and cached) plus iss, aud and exp. Only Keycloak holds the
private key, so a token that passes was issued by Keycloak and hasn't been changed.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

import jwt
import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

log = structlog.get_logger("railbook.auth")

# auto_error=False: return None for a missing header, so we answer 401 (not FastAPI's 403).
# Also adds the "Authorize" button to /docs.
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class User:
    sub: str  # Keycloak's user ID: stable, unlike the username
    username: str
    is_admin: bool


@lru_cache
def jwks_client() -> jwt.PyJWKClient:
    # Caches the key set for 5 min. A token signed with a key it doesn't know yet (Keycloak
    # rotated its keys) makes it fetch the set again, so rotation needs no restart. Those extra
    # fetches are at most one per 30 s (cooldown), so tokens with made-up key IDs can't make
    # the API flood Keycloak.
    # cache_keys: a key, once seen, is kept (per kid, no expiry) until the pod restarts, so
    # logged-in users keep working while Keycloak is down. Trade-off: a key Keycloak removes
    # (e.g. leaked) stays trusted by running pods until they restart.
    return jwt.PyJWKClient(
        get_settings().oidc_jwks_url, cache_keys=True, lifespan=300, timeout=5
    )


def _unauthorized(detail: str) -> HTTPException:
    # RFC 6750: tell the client it needs a (valid) bearer token.
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail,
        headers={"WWW-Authenticate": 'Bearer realm="railbook"'},
    )


def decode(token: str) -> dict:
    settings = get_settings()
    # Which key signed it: the "kid" in the token's header picks one from the JWKS.
    key = jwks_client().get_signing_key_from_jwt(token).key
    return jwt.decode(
        token,
        key,
        # Only RS256, never what the token says it uses: otherwise a forged token could claim
        # "none" (no signature) or HS256 (signed with our PUBLIC key as a shared secret).
        algorithms=["RS256"],
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        leeway=10,  # seconds of clock difference between Keycloak and this pod
    )


def current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    """FastAPI dependency: 401 unless the request carries a valid token."""
    if creds is None:
        raise _unauthorized("login required")
    try:
        claims = decode(creds.credentials)
    except jwt.PyJWKClientConnectionError as exc:
        # Keycloak unreachable and the key isn't cached: not the caller's fault.
        log.error("jwks_unavailable", error=str(exc))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "cannot check tokens") from exc
    except jwt.PyJWTError as exc:
        # Bad signature, expired, wrong issuer/audience, unknown key, not a JWT at all...
        log.info("token_rejected", reason=type(exc).__name__)
        raise _unauthorized("invalid token") from exc

    roles = claims.get("realm_access", {}).get("roles", [])
    return User(
        sub=claims["sub"],
        username=claims.get("preferred_username", ""),
        is_admin=get_settings().oidc_admin_role in roles,
    )


CurrentUser = Annotated[User, Depends(current_user)]

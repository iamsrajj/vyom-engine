"""
auth -- minimal login gate for the beta dashboard.

Username/password pairs are configured in .env (AUTH_USERS=user:pass,user2:pass2),
checked here, and on success a signed session token (JWT, HS256) is issued,
valid for AUTH_SESSION_TTL_HOURS (default 24).

HONEST LIMITATION: this is not a production auth system. Credentials live as
plain user:pass pairs in .env, not a hashed-password user table -- fine for a
handful of trusted beta invitees on a server you control, not fine at any
larger scale. Before opening this beyond a small invite list, replace this
with a real users table + hashed passwords (passlib/bcrypt) and a proper
identity provider.

Two verification paths, because browsers can't attach custom headers to plain
image requests (map tiles):
  - require_auth       -- Authorization: Bearer <token> header. Used by every
                           JSON API endpoint (farms, etc).
  - require_auth_query  -- ?token=<token> query parameter. Used only by the
                           /tiles endpoints, which are loaded by the map
                           library as <img>-style requests with no custom
                           headers available.
"""
import secrets
import string
import time
import uuid
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from vyom.config import settings
from vyom.db import get_db


def _parse_users() -> dict[str, str]:
    users = {}
    for pair in settings.auth_users.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        username, password = pair.split(":", 1)
        users[username.strip()] = password
    return users


def verify_credentials(username: str, password: str) -> bool:
    users = _parse_users()
    expected = users.get(username)
    return expected is not None and expected == password


def issue_token(username: str) -> tuple[str, int]:
    now = int(time.time())
    expires_at = now + settings.auth_session_ttl_hours * 3600
    payload = {"sub": username, "iat": now,
               "exp": expires_at, "typ": "session"}
    token = jwt.encode(payload, settings.auth_secret_key, algorithm="HS256")
    return token, expires_at


REGISTRATION_TOKEN_TTL_MINUTES = 15


def issue_registration_token(google_sub: str, email: str, name: str, picture: Optional[str]) -> str:
    """Short-lived token proving Google identity was already verified server-side
    for THIS signup attempt, so the multi-step signup form (org/designation/
    address/phone+OTP) doesn't need to re-verify Google on every step. Tagged
    typ="registration" so it can never be accepted anywhere require_auth is
    used -- it is not a session token and grants no API access."""
    now = int(time.time())
    payload = {
        "sub": google_sub, "email": email, "name": name, "picture": picture,
        "iat": now, "exp": now + REGISTRATION_TOKEN_TTL_MINUTES * 60,
        "typ": "registration",
    }
    return jwt.encode(payload, settings.auth_secret_key, algorithm="HS256")


def decode_registration_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token, settings.auth_secret_key, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            401, "Signup session expired, please start again with Google sign-in")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid signup session")
    if payload.get("typ") != "registration":
        raise HTTPException(401, "Invalid signup session")
    return payload


def generate_account_id() -> str:
    """Short human-friendly account id, e.g. 'AGD-7F3K2Q'. Uniqueness against
    the users table is enforced by the caller (retry on IntegrityError), not
    here -- this function has no DB access."""
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"AGD-{suffix}"


def _decode(token: str) -> str:
    try:
        payload = jwt.decode(
            token, settings.auth_secret_key, algorithms=["HS256"])
        if payload.get("typ") != "session":
            raise HTTPException(401, "Invalid session")
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired, please sign in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid session")


def require_auth(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    return _decode(authorization[len("Bearer "):])


def require_auth_query(token: Optional[str] = Query(None)) -> str:
    if not token:
        raise HTTPException(401, "Missing token")
    return _decode(token)


def require_error_panel_access(authorization: Optional[str] = Header(None),
                               db: Session = Depends(get_db)) -> str:
    """Gate for the Errors panel and the prewarm admin tool.

    SECURITY FIX (was fail-open): the previous version checked `sub` against
    a comma-separated ERROR_PANEL_USERNAMES allowlist, with `if allowed and
    sub not in allowed`. An empty/unset allowlist made that condition always
    False, silently letting EVERY authenticated user through as if they were
    an admin -- and even when configured, `sub` for a real Google/OTP user is
    a UUID, which can never match a plain username string, making the
    allowlist path permanently unsatisfiable for the primary auth system.

    This now checks a real `role` column on the users table (see models.py),
    defaults to denying access (fail closed) for: no matching user row,
    no role set, or a legacy AUTH_USERS session (sub is not a UUID -- those
    predate the real accounts system and have no role to check). Promoting
    the first real admin is a one-time manual step:
        UPDATE users SET role = 'admin' WHERE email = '<you>';
    """
    from vyom.models import User  # local import: avoids a circular import (models -> db -> ... -> auth)

    sub = require_auth(authorization)
    try:
        user_id = uuid.UUID(sub)
    except ValueError:
        # legacy/non-account session -- no role to check, deny
        raise HTTPException(403, "Not authorized")

    user = db.get(User, user_id)
    if user is None or getattr(user, "role", None) != "admin":
        raise HTTPException(403, "Not authorized")
    return sub


def stable_owner_uuid(sub: str) -> uuid.UUID:
    """Every authenticated identity, from EITHER auth path, mapped to one
    stable UUID used for resource ownership (Polygon.user_id) -- this is
    what farms.py's ownership checks compare against, never a client-
    supplied value (see the BOLA fix in farms.py: user_id used to be taken
    straight from the request body/query string with no relationship to
    who was actually authenticated).

    A real account's `sub` (from Google/OTP signup) already IS the User
    row's own UUID -- returned as-is. A legacy AUTH_USERS session's `sub`
    is a plain username with no Users row at all; uuid5 gives it a
    deterministic, stable, collision-resistant UUID derived from that
    username, so each distinct legacy account still gets consistent,
    isolated ownership without requiring a schema change on that fallback
    path."""
    try:
        return uuid.UUID(sub)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"vyom-legacy-user:{sub}")

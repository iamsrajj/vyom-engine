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
import time
from typing import Optional

import jwt
from fastapi import Header, HTTPException, Query

from vyom.config import settings


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
    payload = {"sub": username, "iat": now, "exp": expires_at}
    token = jwt.encode(payload, settings.auth_secret_key, algorithm="HS256")
    return token, expires_at


def _decode(token: str) -> str:
    try:
        payload = jwt.decode(
            token, settings.auth_secret_key, algorithms=["HS256"])
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


def require_error_panel_access(authorization: Optional[str] = Header(None)) -> str:
    """Gate for the Errors panel. There is no real role system yet (auth_users
    is a flat username:password list, no roles column) -- error_panel_usernames
    in .env is an honest stopgap allowlist, not a real permissions model.
    Replace this with a proper role check once the user-accounts rebuild lands."""
    sub = require_auth(authorization)
    allowed = [u.strip()
               for u in settings.error_panel_usernames.split(",") if u.strip()]
    if allowed and sub not in allowed:
        raise HTTPException(403, "Not authorized to view error logs")
    return sub

"""api_auth -- authentication, the standard error envelope, and rate
limiting for the business partner API (points 2/3/5 of the monetization
spec: /api/v1/farms in vyom/api/partner_farms.py).

Auth model: two headers, X-Api-Key (identifier, safe to log) and
X-Api-Secret (the actual bearer credential, hashed at rest -- see
ApiCredential's docstring in models.py). Every request passes through
require_business_api_auth, which checks FOUR independent gates, in order,
each with its own error code so an integrator can branch on `code` rather
than parsing message text:

  1. AUTH_INVALID    -- key/secret missing, unknown, wrong, or revoked
  2. BUSINESS_INACTIVE -- the ₹999/year maintenance subscription isn't
                          currently active (see vyom/api/billing.py)
  3. PAYMENT_DUE     -- a monthly per-acre API invoice is overdue past its
                        15-day grace window (see vyom/billing_tasks.py) --
                        this is the "lapsed invoice restricts API access
                        only" behavior confirmed earlier
  4. RATE_LIMITED     -- too many requests in the current window

Nothing here rolls a hard failure into a generic 500 -- every gate is a
deliberate, documented outcome, which is what "never fails" actually means
in practice (see the spec's §5 reliability notes): fails safely and
predictably, not silently.
"""
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import redis
from fastapi import Depends, Header, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.config import settings
from vyom.db import get_db
from vyom.models import ApiCredential, BusinessApiInvoice, User

logger = logging.getLogger("vyom.api_auth")

_redis = redis.from_url(settings.redis_url, decode_responses=True)


class ApiV1Error(Exception):
    """Raised anywhere in the partner-API code path; caught by the
    exception handler registered in vyom/api/main.py, which renders the
    standard {"error": {"code", "message", "retriable"}} envelope. message
    is always safe to show directly to an integrator."""

    def __init__(self, status_code: int, code: str, message: str, retriable: bool = False):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retriable = retriable
        super().__init__(message)


@dataclass
class BusinessApiContext:
    user: User
    credential: ApiCredential
    warnings: list[str] = field(default_factory=list)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_credential(db: Session, *, user: User, label: str | None = None) -> tuple[ApiCredential, str]:
    """Creates a new key/secret pair. Returns (credential, raw_secret) --
    raw_secret is NEVER stored; this is the only moment it exists in
    plaintext anywhere. Caller (vyom/api/business_api_credentials.py) is
    responsible for returning it to the user exactly once."""
    api_key = "vyom_key_" + secrets.token_urlsafe(18)
    api_secret = secrets.token_urlsafe(32)
    credential = ApiCredential(
        user_id=user.id, api_key=api_key, api_secret_hash=_hash_secret(
            api_secret),
        secret_last4=api_secret[-4:], label=label, status="active",
    )
    db.add(credential)
    db.commit()
    db.refresh(credential)
    return credential, api_secret


def _check_rate_limit(api_key: str, response: Response) -> None:
    """Fixed-window limiter (settings.api_rate_limit_per_minute per key),
    shared across all app workers via Redis, same client pattern as
    vyom/cdse_rate_limiter.py. Fails OPEN on a Redis outage -- a monitoring
    problem on our side should never be the reason a paying integrator's
    calls start failing; it's logged so it's visible, not silent."""
    window = int(time.time() // 60)
    redis_key = f"api_rate:{api_key}:{window}"
    limit = settings.api_rate_limit_per_minute
    try:
        count = _redis.incr(redis_key)
        if count == 1:
            _redis.expire(redis_key, 65)
    except redis.RedisError:
        logger.warning(
            "Rate limiter Redis unavailable -- failing open for this request")
        return

    remaining = max(0, limit - count)
    reset_at = (window + 1) * 60
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(reset_at)

    if count > limit:
        raise ApiV1Error(429, "RATE_LIMITED",
                         f"Rate limit of {limit} requests/minute exceeded. Retry after "
                         f"{reset_at - int(time.time())}s.", retriable=True)


def _compute_warnings(db: Session, user: User) -> list[str]:
    """Surfaced in every successful response's meta.warnings, per the
    spec's 'don't wait for a hard failure to say something's wrong'
    requirement -- these are non-fatal, forward-looking notices."""
    warnings = []
    now = datetime.now(timezone.utc)

    if user.business_expires_at:
        days_left = (user.business_expires_at - now).days
        if 0 <= days_left <= 7:
            warnings.append("business_subscription_expiring_soon")

    upcoming = db.execute(
        select(BusinessApiInvoice).where(
            BusinessApiInvoice.user_id == user.id, BusinessApiInvoice.status == "pending")
    ).scalars().first()
    if upcoming and upcoming.due_at:
        days_to_due = (upcoming.due_at - now).days
        if 0 <= days_to_due <= 3:
            warnings.append("payment_due_soon")

    return warnings


def require_business_api_auth(
    request: Request,
    response: Response,
    x_api_key: str = Header(default=None, alias="X-Api-Key"),
    x_api_secret: str = Header(default=None, alias="X-Api-Secret"),
    db: Session = Depends(get_db),
) -> BusinessApiContext:
    if not x_api_key or not x_api_secret:
        raise ApiV1Error(401, "AUTH_INVALID",
                         "Missing X-Api-Key/X-Api-Secret headers")

    credential = db.execute(select(ApiCredential).where(
        ApiCredential.api_key == x_api_key)).scalar_one_or_none()
    # constant-time comparison against a hash of the SUPPLIED secret either
    # way (even when no credential/hash exists), so a missing api_key and a
    # wrong api_secret take the same amount of time -- prevents key
    # enumeration via response-time side channel.
    supplied_hash = _hash_secret(x_api_secret)
    stored_hash = credential.api_secret_hash if credential else _hash_secret(
        "no-such-key")
    if credential is None or not hmac.compare_digest(stored_hash, supplied_hash):
        raise ApiV1Error(401, "AUTH_INVALID", "Invalid API key or secret")
    # From here on, a credential row is known -- record it on request.state
    # immediately (before any further gate can reject the request) so the
    # audit-log middleware in vyom/api/main.py can attribute even a
    # REJECTED call (revoked key, business inactive, payment due, rate
    # limited) to the right credential/user, not just successful ones.
    request.state.api_credential_id = credential.id
    request.state.api_user_id = credential.user_id

    if credential.status != "active":
        raise ApiV1Error(
            401, "KEY_REVOKED", "This API key has been revoked. Generate a new one from your business dashboard.")

    user = db.get(User, credential.user_id)
    if user is None or user.account_type != "business":
        raise ApiV1Error(401, "AUTH_INVALID",
                         "This credential is not attached to a business account")
    if user.business_status != "active":
        raise ApiV1Error(
            403, "BUSINESS_INACTIVE",
            "Your business account's ₹999/year maintenance subscription is not active. "
            "Renew it from your dashboard to restore API access.")
    if user.business_api_payment_status == "suspended":
        raise ApiV1Error(
            402, "PAYMENT_DUE",
            "API access is suspended due to an overdue invoice. Pay the outstanding invoice to restore access.")

    _check_rate_limit(x_api_key, response)

    credential.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return BusinessApiContext(user=user, credential=credential,
                              warnings=_compute_warnings(db, user))

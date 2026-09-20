"""idempotency -- Idempotency-Key support for the partner API's mutating
endpoints (POST/PATCH in vyom/api/partner_farms.py). A retried request with
the same key (network blip, integrator's own retry logic) returns the
ORIGINAL response instead of creating a second farm or reapplying an
update twice.
"""
import hashlib
import json
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.api_auth import ApiV1Error
from vyom.models import ApiIdempotencyKey


def request_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def check_and_replay(db: Session, *, credential_id, idempotency_key: Optional[str],
                     body: dict) -> Optional[tuple[int, dict]]:
    """Call at the top of a mutating endpoint, before doing any real work.
    Returns (status_code, body) to replay verbatim if this exact
    (credential, key, body) was already handled -- the endpoint should
    return that immediately. Returns None if this is a new request (the
    endpoint should proceed normally, then call store_result() before
    returning). Raises ApiV1Error(409, ...) if the same key was reused with
    a DIFFERENT body -- that's a client bug, not a safe retry, and silently
    returning the old response would be actively misleading."""
    if not idempotency_key:
        return None

    req_hash = request_hash(body)
    existing = db.execute(
        select(ApiIdempotencyKey).where(
            ApiIdempotencyKey.api_credential_id == credential_id,
            ApiIdempotencyKey.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    if existing.request_hash != req_hash:
        raise ApiV1Error(
            409, "IDEMPOTENCY_KEY_REUSED",
            "This Idempotency-Key was already used with a different request body. "
            "Use a new key for a different request.")
    return existing.response_status, existing.response_body


def store_result(db: Session, *, credential_id, idempotency_key: Optional[str],
                 body: dict, status_code: int, response_body: dict) -> None:
    """No-op if no idempotency key was supplied -- callers aren't required
    to send one, but get no replay protection without it."""
    if not idempotency_key:
        return
    db.add(ApiIdempotencyKey(
        api_credential_id=credential_id, idempotency_key=idempotency_key,
        request_hash=request_hash(body), response_status=status_code, response_body=response_body,
    ))
    db.commit()

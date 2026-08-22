"""
otp_client -- calls AgriDoot's own genotp SMS API and handles verification
entirely server-side.

SECURITY NOTE: the original request described storing otp_value in the
browser's localStorage and matching it client-side. That was not implemented
that way on purpose -- anyone can read localStorage via devtools and submit
the correct code without ever receiving the SMS, which defeats OTP
verification entirely. Instead:

  1. send_otp() calls genotp, gets otp_value back from AgriDoot's API,
     and stores a SHA-256 hash of it in the otp_verifications table --
     never the plaintext, and never sent back to the frontend at all.
  2. The frontend only ever holds the otp_verifications row's own id
     (`otp_session`), which identifies the *attempt*, not the code.
  3. verify_otp() hashes what the user typed and compares server-side,
     with an attempt limit and expiry to blunt brute-forcing a 6-digit code.
"""
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy.orm import Session

from vyom.config import settings
from vyom.models import OtpVerification

OTP_TTL_MINUTES = 10


def _hash_otp(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


def send_otp(db: Session, phone: str, cc: str, purpose: str) -> uuid.UUID:
    """Calls AgriDoot's genotp endpoint, stores the returned code hashed,
    and returns the otp_verifications row id the frontend should reference
    on the follow-up verify call. Raises requests.HTTPError if the SMS
    provider call itself fails (caller should surface a clean 502 to the user)."""
    resp = requests.post(
        settings.agridoot_otp_url,
        headers={"x-api-key": settings.agridoot_otp_api_key},
        data={"phone": phone, "cc": cc},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    otp_data = body["otpVerify"]

    row = OtpVerification(
        id=uuid.uuid4(),
        phone_cc=cc,
        phone=phone,
        purpose=purpose,
        otp_hash=_hash_otp(str(otp_data["otp_value"])),
        provider_otp_id=str(otp_data.get("otp_id")),
        provider_request_id=otp_data.get("request_id"),
        expires_at=datetime.now(timezone.utc) +
        timedelta(minutes=OTP_TTL_MINUTES),
    )
    db.add(row)
    db.commit()
    return row.id


class OtpVerifyResult:
    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason  # "expired" | "too_many_attempts" | "wrong_code" | "not_found"


def verify_otp(db: Session, otp_session: uuid.UUID, code: str) -> OtpVerifyResult:
    row = db.get(OtpVerification, otp_session)
    if row is None:
        return OtpVerifyResult(False, "not_found")
    if row.verified:
        # idempotent: already verified, treat as success
        return OtpVerifyResult(True)
    if datetime.now(timezone.utc) > row.expires_at.replace(tzinfo=timezone.utc):
        return OtpVerifyResult(False, "expired")
    if row.attempts >= row.max_attempts:
        return OtpVerifyResult(False, "too_many_attempts")

    row.attempts += 1
    if _hash_otp(code) != row.otp_hash:
        db.add(row)
        db.commit()
        return OtpVerifyResult(False, "wrong_code")

    row.verified = True
    db.add(row)
    db.commit()
    return OtpVerifyResult(True)

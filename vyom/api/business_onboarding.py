"""api/business_onboarding -- the two prerequisites required before a
business-account upgrade payment is accepted (point 6 of the monetization
spec): (1) a GSTIN verified as Active, with its legal name/address saved
as the account's company details, and (2) a business email -- distinct
from a free consumer provider and from the account's own login email --
verified via an OTP emailed through our own SMTP.

vyom/api/billing.py's create_business_upgrade_order checks BOTH
user.gst_verified_at and user.business_email_verified_at are set before
it will create a Razorpay order at all.
"""
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.auth import require_auth, stable_owner_uuid
from vyom.config import settings
from vyom.db import get_db
from vyom.email_utils import render_email, send_email
from vyom.gst_verification import GstVerificationError, verify_gstin
from vyom.models import BusinessEmailOtp, User

logger = logging.getLogger("vyom.api.business_onboarding")

router = APIRouter(prefix="/business/onboarding", tags=["business"])

# Deliberately not pydantic's EmailStr (which needs the extra
# email-validator dependency this project doesn't otherwise use) -- a
# simple, permissive shape check is sufficient here since the OTP step
# itself is the real verification that the address is reachable.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(value: str) -> str:
    value = value.strip().lower()
    if not _EMAIL_PATTERN.match(value):
        raise ValueError("Enter a valid email address")
    return value


def _get_user(db: Session, current_user: str) -> User:
    user = db.get(User, stable_owner_uuid(current_user))
    if user is None:
        raise HTTPException(
            400, "Full account required (Google or phone sign-in).")
    return user


# ---------------------------------------------------------------------------
# GST verification
# ---------------------------------------------------------------------------

class VerifyGstRequest(BaseModel):
    gstin: str


class VerifyGstOut(BaseModel):
    gstin: str
    company_legal_name: str
    company_registered_address: Optional[str]
    verified_at: datetime


@router.post("/verify-gst", response_model=VerifyGstOut)
def verify_gst(payload: VerifyGstRequest, current_user: str = Depends(require_auth),
               db: Session = Depends(get_db)):
    user = _get_user(db, current_user)
    try:
        result = verify_gstin(payload.gstin)
    except GstVerificationError as exc:
        raise HTTPException(400, str(exc))

    user.gstin = result.gstin
    user.company_legal_name = result.legal_name
    user.company_registered_address = result.address
    user.gst_verified_at = datetime.now(timezone.utc)
    db.commit()

    return VerifyGstOut(
        gstin=user.gstin, company_legal_name=user.company_legal_name,
        company_registered_address=user.company_registered_address, verified_at=user.gst_verified_at,
    )


# ---------------------------------------------------------------------------
# Business email verification
# ---------------------------------------------------------------------------

def _hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


def _free_email_domains() -> set[str]:
    return {d.strip().lower() for d in settings.free_email_domains.split(",") if d.strip()}


class StartBusinessEmailRequest(BaseModel):
    email: str

    _validate = field_validator("email")(_validate_email)


@router.post("/business-email/start")
def start_business_email_verification(payload: StartBusinessEmailRequest,
                                      current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    user = _get_user(db, current_user)
    email = payload.email.strip().lower()
    domain = email.rsplit("@", 1)[-1]

    if domain in _free_email_domains():
        raise HTTPException(
            400, "Please use a business email address, not a personal Gmail/Yahoo/Outlook-type address.")
    if user.email and email == user.email.strip().lower():
        raise HTTPException(
            400, "Your business email must be different from your account's login email.")

    otp = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.now(timezone.utc) + \
        timedelta(minutes=settings.business_email_otp_ttl_minutes)
    db.add(BusinessEmailOtp(user_id=user.id, email=email,
                            otp_hash=_hash_otp(otp), expires_at=expires_at))
    db.commit()

    try:
        send_email(
            to=email,
            subject="Verify your business email -- Vyom Engine",
            html_body=render_email(
                preheader="Your Vyom Engine business email verification code",
                heading="Verify your business email",
                body_html=f"<p>Your verification code is:</p>"
                f"<p style='font-size:28px; font-weight:700; letter-spacing:4px;'>{otp}</p>"
                f"<p>This code expires in {settings.business_email_otp_ttl_minutes} minutes. "
                f"If you didn't request this, you can safely ignore this email.</p>",
            ),
            text_fallback=f"Your Vyom Engine business email verification code is {otp}. "
            f"It expires in {settings.business_email_otp_ttl_minutes} minutes.",
        )
    except Exception as exc:  # noqa: BLE001 -- see vyom/billing_tasks.py's same pattern
        logger.error("Failed to send business-email OTP to %s: %s", email, exc)
        raise HTTPException(
            502, "Could not send the verification email right now. Please try again.")

    return {"status": "sent", "email": email, "expires_in_minutes": settings.business_email_otp_ttl_minutes}


class VerifyBusinessEmailRequest(BaseModel):
    email: str
    otp: str

    _validate = field_validator("email")(_validate_email)


@router.post("/business-email/verify")
def verify_business_email(payload: VerifyBusinessEmailRequest, current_user: str = Depends(require_auth),
                          db: Session = Depends(get_db)):
    user = _get_user(db, current_user)
    email = payload.email.strip().lower()

    record = db.execute(
        select(BusinessEmailOtp).where(
            BusinessEmailOtp.user_id == user.id, BusinessEmailOtp.email == email,
            BusinessEmailOtp.verified_at.is_(None),
        ).order_by(BusinessEmailOtp.created_at.desc())
    ).scalars().first()

    if record is None:
        raise HTTPException(
            400, "No pending verification for this email. Request a new code first.")
    if datetime.now(timezone.utc) > record.expires_at:
        raise HTTPException(
            400, "This code has expired. Request a new one.")
    if record.attempts >= settings.business_email_otp_max_attempts:
        raise HTTPException(
            429, "Too many incorrect attempts. Request a new code.")

    if not hmac.compare_digest(record.otp_hash, _hash_otp(payload.otp.strip())):
        record.attempts += 1
        db.commit()
        raise HTTPException(400, "Incorrect code. Please try again.")

    record.verified_at = datetime.now(timezone.utc)
    user.business_email = email
    user.business_email_verified_at = record.verified_at
    db.commit()

    return {"status": "verified", "business_email": email}

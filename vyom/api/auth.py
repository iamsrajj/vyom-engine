import uuid

import requests as requests_lib
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vyom.auth import (
    verify_credentials, issue_token,
    issue_registration_token, decode_registration_token, generate_account_id,
    require_auth,
)
from vyom.db import get_db
from vyom.error_log import log_error
from vyom.google_auth import verify_google_id_token
from vyom.models import User, OtpVerification
from vyom.otp_client import send_otp, verify_otp

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------- legacy ---
class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(payload: LoginRequest):
    """Kept as a fallback for the original beta invite list (AUTH_USERS in
    .env). New accounts should use /auth/google + phone verification instead
    -- see the rest of this file."""
    if not verify_credentials(payload.username, payload.password):
        raise HTTPException(401, "Invalid username or password")
    token, expires_at = issue_token(payload.username)
    return {"token": token, "expires_at": expires_at, "username": payload.username}


# --------------------------------------------------------- Google sign-in ---
class GoogleAuthRequest(BaseModel):
    id_token: str


@router.post("/google")
def google_auth(payload: GoogleAuthRequest, db: Session = Depends(get_db)):
    """The one entry point for both 'Continue with Google' flows. Verifies
    the ID token server-side (never trusts client-supplied identity claims),
    then checks whether this Google account already has a complete profile:
      - existing user  -> real session token, status="signin"
      - new user       -> short-lived registration token + Google's
                           name/email/picture to prefill the signup form,
                           status="signup_required"
    This is what "check on system whether it is signin or signup" means in
    practice -- the frontend doesn't decide, the backend does, based on
    whether a users row exists for this google_sub."""
    try:
        identity = verify_google_id_token(payload.id_token)
    except ValueError as exc:
        log_error("auth", f"Google ID token verification failed: {exc}")
        raise HTTPException(401, "Could not verify Google sign-in")

    user = db.query(User).filter(User.google_sub == identity.sub).first()
    if user is not None:
        token, expires_at = issue_token(str(user.id))
        return {
            "status": "signin",
            "token": token,
            "expires_at": expires_at,
            "user": _user_out(user),
        }

    registration_token = issue_registration_token(
        identity.sub, identity.email, identity.name, identity.picture,
    )
    return {
        "status": "signup_required",
        "registration_token": registration_token,
        "prefill": {"name": identity.name, "email": identity.email, "picture": identity.picture},
    }


# -------------------------------------------------------------------- OTP ---
class SendOtpRequest(BaseModel):
    phone: str
    cc: str = "91"
    # "signup" while completing a new registration_token'd account,
    # "signin" when an existing user is authenticating by phone alone.
    purpose: str


@router.post("/otp/send")
def send_otp_endpoint(payload: SendOtpRequest, db: Session = Depends(get_db)):
    if payload.purpose not in ("signup", "signin"):
        raise HTTPException(400, "purpose must be 'signup' or 'signin'")

    if payload.purpose == "signup":
        existing = db.query(User).filter(User.phone == payload.phone).first()
        if existing is not None:
            raise HTTPException(
                409, "This phone number is already registered to an account")
    else:  # signin
        existing = db.query(User).filter(User.phone == payload.phone).first()
        if existing is None:
            raise HTTPException(
                404, "No account found for this phone number. Please sign up first.")

    try:
        otp_session = send_otp(db, payload.phone, payload.cc, payload.purpose)
    except requests_lib.RequestException as exc:
        log_error("auth", f"genotp API call failed: {exc}", context={
                  "phone": payload.phone})
        raise HTTPException(
            502, "Could not send OTP right now, please try again shortly")

    return {"otp_session": str(otp_session)}


class VerifyOtpRequest(BaseModel):
    otp_session: uuid.UUID
    code: str


@router.post("/otp/verify")
def verify_otp_endpoint(payload: VerifyOtpRequest, db: Session = Depends(get_db)):
    """Used directly for phone-based SIGN-IN (existing account). For SIGNUP,
    the frontend calls this same endpoint first to confirm the code, then
    calls /auth/signup/complete with the now-verified otp_session to actually
    create the account -- kept as two calls so a wrong code never partially
    creates a user row."""
    result = verify_otp(db, payload.otp_session, payload.code)
    if not result.ok:
        messages = {
            "expired": "This code has expired, please request a new one",
            "too_many_attempts": "Too many incorrect attempts, please request a new code",
            "wrong_code": "Incorrect code",
            "not_found": "OTP session not found, please request a new code",
        }
        raise HTTPException(400, messages.get(
            result.reason, "Verification failed"))

    row = db.get(OtpVerification, payload.otp_session)

    if row.purpose == "signin":
        user = db.query(User).filter(User.phone == row.phone).first()
        if user is None:  # phone existed at send-time but was deleted mid-flow; edge case
            raise HTTPException(404, "No account found for this phone number")
        token, expires_at = issue_token(str(user.id))
        return {"status": "signin", "token": token, "expires_at": expires_at, "user": _user_out(user)}

    # signup purpose: frontend proceeds to /auth/signup/complete
    return {"status": "otp_verified"}


# ---------------------------------------------------------- signup finish ---
class CompleteSignupRequest(BaseModel):
    registration_token: str
    organization: str
    designation: str
    address: str
    phone: str
    phone_cc: str = "91"
    # must reference an already-verified signup OTP for this same phone
    otp_session: uuid.UUID


@router.post("/signup/complete")
def complete_signup(payload: CompleteSignupRequest, db: Session = Depends(get_db)):
    identity = decode_registration_token(payload.registration_token)

    otp_row = db.get(OtpVerification, payload.otp_session)
    if (otp_row is None or not otp_row.verified or otp_row.purpose != "signup"
            or otp_row.phone != payload.phone):
        raise HTTPException(
            400, "Phone number is not verified for this signup attempt")

    if db.query(User).filter(User.phone == payload.phone).first() is not None:
        raise HTTPException(
            409, "This phone number is already registered to an account")
    if db.query(User).filter(User.google_sub == identity["sub"]).first() is not None:
        raise HTTPException(
            409, "An account already exists for this Google identity")

    # generate_account_id() has no DB access -- retry on the rare collision
    for _ in range(5):
        try:
            user = User(
                id=uuid.uuid4(),
                account_id=generate_account_id(),
                email=identity["email"],
                google_sub=identity["sub"],
                name=identity["name"],
                profile_img_url=identity.get("picture"),
                organization=payload.organization,
                designation=payload.designation,
                address=payload.address,
                phone_cc=payload.phone_cc,
                phone=payload.phone,
                phone_verified=True,
            )
            db.add(user)
            db.commit()
            break
        except IntegrityError as exc:
            db.rollback()
            if "account_id" not in str(exc.orig):
                raise HTTPException(409, "Account already exists") from exc
            # account_id collision (extremely rare) -- retry with a new one
            continue
    else:
        raise HTTPException(
            500, "Could not generate a unique account id, please try again")

    token, expires_at = issue_token(str(user.id))
    return {"status": "signin", "token": token, "expires_at": expires_at, "user": _user_out(user)}


# ------------------------------------------------------------------- /me ---
@router.get("/me")
def me(user_id: str = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        # Legacy AUTH_USERS session (sub is a plain username, not a user uuid)
        raise HTTPException(404, "No profile for this session type")
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(404, "User not found")
    return _user_out(user)


def _user_out(user: User) -> dict:
    return {
        "account_id": user.account_id,
        "email": user.email,
        "name": user.name,
        "profile_img_url": user.profile_img_url,
        "organization": user.organization,
        "designation": user.designation,
        "address": user.address,
        "phone": user.phone,
        "phone_cc": user.phone_cc,
    }

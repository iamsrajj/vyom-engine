"""
contact.py -- the "Contact Us" form on the dashboard's Help modal (see
web/index.html's Help & Support modal). Sends a plain email via Gmail SMTP
using an App Password.

IMPORTANT about the credential: Google no longer allows SMTP login with a
regular account password. settings.smtp_app_password must be a Gmail "App
Password" -- a 16-character code generated separately at
myaccount.google.com/apppasswords (requires 2-Step Verification enabled on
that Google account first), scoped to this one integration and revocable
independently of the account's real password.

This endpoint is intentionally NOT behind require_auth -- someone locked
out of their account or a prospective user should still be able to reach
support. That does mean it's a public, unauthenticated endpoint that
triggers an outbound email on every call: there's no rate-limiting or CAPTCHA
here yet, so it could be spammed. Worth adding basic rate-limiting (e.g. by
IP) if this turns out to attract abuse in practice.
"""
import logging
import smtplib
from email.mime.text import MIMEText

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from vyom.config import settings

logger = logging.getLogger("vyom.contact")
router = APIRouter(prefix="/support", tags=["support"])


class ContactRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    # Deliberately a plain string, not EmailStr -- the form also accepts a
    # phone number as a contact method, not just email.
    contact: str = Field(..., min_length=3, max_length=200)
    message: str = Field(..., min_length=1, max_length=5000)


@router.post("/contact")
def submit_contact(payload: ContactRequest):
    if not settings.smtp_username or not settings.smtp_app_password:
        raise HTTPException(
            503, "The contact form isn't configured on the server yet (missing SMTP credentials in .env).")

    recipients = [addr.strip() for addr in settings.support_email_recipients.split(
        ",") if addr.strip()]
    if not recipients:
        raise HTTPException(
            503, "The contact form has no configured recipient (SUPPORT_EMAIL_RECIPIENTS is empty).")

    body = (
        f"New Vyom Engine support request\n\n"
        f"Name: {payload.name}\n"
        f"Contact: {payload.contact}\n\n"
        f"Message:\n{payload.message}\n"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Vyom Engine support request from {payload.name}"
    msg["From"] = settings.smtp_username
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            server.starttls()
            server.login(settings.smtp_username, settings.smtp_app_password)
            server.sendmail(settings.smtp_username,
                            recipients, msg.as_string())
    except smtplib.SMTPException as exc:
        logger.error("Failed to send contact form email: %s", exc)
        raise HTTPException(
            502, "Could not send your message right now -- please try again later or email support directly.")

    return {"sent": True}

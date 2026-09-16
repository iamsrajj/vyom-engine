"""
contact.py -- the "Contact Us" form on the dashboard's Help modal (see
web/index.html's Help & Support modal). Sends a themed HTML email via
Gmail SMTP (see vyom/email_utils.py for the shared send/template logic).

IMPORTANT about the credential: Google no longer allows SMTP login with a
regular account password. settings.smtp_app_password must be a Gmail "App
Password" -- a 16-character code generated separately at
myaccount.google.com/apppasswords (requires 2-Step Verification enabled on
that Google account first), scoped to this one integration and revocable
independently of the account's real password.

This endpoint is intentionally NOT behind require_auth -- someone locked
out of their account or a prospective user should still be able to reach
support. It uses optional_auth instead: if the caller happens to already be
signed in, their submission also gets an in-app "message sent" confirmation
notification; if not, the email still sends, just with no notification to
attach it to. This does mean it's a public, unauthenticated endpoint that
triggers an outbound email on every call: there's no rate-limiting or
CAPTCHA here yet, so it could be spammed. Worth adding basic rate-limiting
(e.g. by IP) if this turns out to attract abuse in practice.
"""
import html
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from vyom.auth import optional_auth, stable_owner_uuid
from vyom.config import settings
from vyom.db import get_db
from vyom.email_utils import send_email, render_email, EmailSendError
from vyom.notifications import notify_contact_status

logger = logging.getLogger("vyom.contact")
router = APIRouter(prefix="/support", tags=["support"])


class ContactRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., min_length=3, max_length=200)
    # Optional -- only Name, Email, and Message are required on the form.
    phone: str | None = Field(None, max_length=50)
    message: str = Field(..., min_length=1, max_length=5000)


@router.post("/contact")
def submit_contact(payload: ContactRequest, current_user: str | None = Depends(optional_auth),
                   db: Session = Depends(get_db)):
    recipients = [addr.strip()
                  for addr in settings.support_email_recipients.split(",") if addr.strip()]
    if not recipients:
        raise HTTPException(
            503, "The contact form has no configured recipient (SUPPORT_EMAIL_RECIPIENTS is empty).")

    name_esc = html.escape(payload.name)
    email_esc = html.escape(payload.email)
    phone_esc = html.escape(
        payload.phone) if payload.phone else "(not provided)"
    message_esc = html.escape(payload.message).replace("\n", "<br>")

    body_html = (
        f"<p><b>Name:</b> {name_esc}<br>"
        f"<b>Email:</b> {email_esc}<br>"
        f"<b>Phone:</b> {phone_esc}</p>"
        f"<p><b>Message:</b><br>{message_esc}</p>"
    )
    text_fallback = (
        f"New Vyom Engine support request\n\n"
        f"Name: {payload.name}\nEmail: {payload.email}\nPhone: {payload.phone or '(not provided)'}\n\n"
        f"Message:\n{payload.message}\n"
    )

    try:
        send_email(
            recipients,
            f"Vyom Engine support request from {payload.name}",
            render_email(
                preheader=payload.message[:150],
                heading="New support request",
                body_html=body_html,
            ),
            text_fallback,
        )
    except EmailSendError as exc:
        logger.error("Failed to send contact form email: %s", exc)
        raise HTTPException(
            502, "Could not send your message right now -- please try again later or email support directly.")

    if current_user:
        try:
            notify_contact_status(
                db, stable_owner_uuid(current_user), payload.name)
        except Exception:  # noqa: BLE001 -- the email already sent successfully; a
            # notification-row failure must never turn that into an error response.
            logger.exception(
                "Failed to create contact_status notification for user %s", current_user)

    return {"sent": True}

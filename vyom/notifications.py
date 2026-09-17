"""
notifications -- creation logic for every notification type: field_ready,
new_reading, refresh_complete, stale_data, contact_status, admin_alert.

Each function writes one (or more, for admin_alert) Notification row for
the in-app panel, and the types that also email (field_ready, new_reading,
stale_data, admin_alert) send a themed email via vyom/email_utils.py.
Email sending here is always best-effort: a failure is logged and the
notification row is left with email_sent=false, but never raises past the
caller -- a notification failing to send must never break the pipeline
task or request that triggered it.
"""
import html
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.config import settings
from vyom.email_utils import send_email, render_email, EmailSendError
from vyom.models import Notification, Polygon, User

logger = logging.getLogger("vyom.notifications")


def _create(db: Session, *, user_id, type_: str, title: str, body: str,
            farm_id=None, context: dict | None = None) -> Notification:
    row = Notification(
        user_id=user_id, farm_id=farm_id, type=type_, title=title, body=body,
        context=context or {},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _send_email_best_effort(to, subject: str, html_body: str, text_fallback: str) -> bool:
    try:
        send_email(to, subject, html_body, text_fallback)
        return True
    except EmailSendError as exc:
        logger.warning("Notification email not sent (%s): %s", subject, exc)
        return False


def _mark_emailed(db: Session, row: Notification) -> None:
    row.email_sent = True
    db.add(row)
    db.commit()


def _has_notification(db: Session, farm_id, type_: str) -> bool:
    return db.execute(
        select(Notification.id).where(
            Notification.farm_id == farm_id, Notification.type == type_).limit(1)
    ).scalar_one_or_none() is not None


def notify_farm_data_update(db: Session, farm: Polygon, platform: str, had_data_before: bool = False) -> None:
    """Called from tasks.fill_gaps_callback whenever a refresh round for
    this farm+platform landed >=1 new real reading.

    is_first_ever requires BOTH:
      (a) had_data_before is False -- the farm had zero real readings
          before this refresh round even STARTED, computed by the caller
          once at the top of tasks.refresh_farm. This is what actually
          matters: checking only "does a field_ready notification already
          exist" was the bug -- the notifications table starts empty for
          every farm regardless of age, so the first refresh any
          pre-existing farm got after this system shipped incorrectly said
          "your field is ready" for farms that had had real data for months.
      (b) no field_ready notification has been sent for this farm yet --
          guards the rarer case of S1 and S2 both landing a farm's very
          first real data within the same initial refresh call (normal for
          farm creation, which requests both platforms at once); whichever
          platform's callback commits first is the only one that should
          call it "field_ready" rather than both firing independently."""
    is_first_ever = (not had_data_before) and not _has_notification(
        db, farm.id, "field_ready")
    name = farm.name or "Your field"
    name_esc = html.escape(name)
    dashboard_url = f"{settings.dashboard_base_url}/"

    if is_first_ever:
        notif_type = "field_ready"
        title = f"{name} is ready"
        body = f'We\'ve finished fetching satellite history for "{name}" -- open it to see your first readings.'
        subject = f"Your field is ready: {name}"
        heading = "Your field is ready!"
        body_html = (
            f"<p>Good news -- we've finished pulling satellite history for <b>{name_esc}</b>. "
            f"NDVI and your other indices are ready to view.</p>"
        )
    else:
        notif_type = "new_reading"
        title = f"New reading available for {name}"
        body = f'A new satellite pass has been processed for "{name}".'
        subject = f"New satellite reading: {name}"
        heading = "New reading available"
        body_html = (
            f"<p>A new satellite pass just finished processing for <b>{name_esc}</b>. "
            f"Open your field to see the latest reading.</p>"
        )

    row = _create(
        db, user_id=farm.user_id, type_=notif_type, title=title, body=body,
        farm_id=farm.id, context={
            "farm_name": farm.name, "platform": platform},
    )

    user = db.get(User, farm.user_id)
    if user and user.email:
        sent = _send_email_best_effort(
            user.email, subject,
            render_email(preheader=body, heading=heading, body_html=body_html,
                         cta_label="Open Vyom Engine", cta_url=dashboard_url),
            text_fallback=f"{body}\n\n{dashboard_url}",
        )
        if sent:
            _mark_emailed(db, row)


def notify_refresh_complete(db: Session, farm: Polygon) -> None:
    """In-panel only, no email. Fired when a MANUAL refresh ("Load Data")
    finishes finding nothing new -- if it DID find something new,
    notify_farm_data_update already covers that (with an email); this just
    makes sure a manual refresh never goes silent when the honest answer
    is "checked, nothing's changed yet"."""
    name = farm.name or "your field"
    _create(
        db, user_id=farm.user_id, type_="refresh_complete",
        title=f"Checked {name} for new imagery",
        body="No new satellite pass since your last update.",
        farm_id=farm.id,
    )


def notify_stale_data(db: Session, farm: Polygon, days_since: int) -> None:
    """Fired from poll_all_farms when a farm's most recent REAL reading is
    older than settings.stale_data_threshold_days. De-duping which farms
    actually get called here (so this doesn't refire every 6h forever once
    a farm goes stale) is the caller's job -- see poll_all_farms."""
    name = farm.name or "Your field"
    name_esc = html.escape(name)
    dashboard_url = f"{settings.dashboard_base_url}/"
    body = f'No new satellite pass for "{name}" in {days_since} days -- showing the last known reading.'
    subject = f"No new imagery for {name} in {days_since} days"

    row = _create(
        db, user_id=farm.user_id, type_="stale_data",
        title=f"{name}: no new imagery in {days_since} days",
        body=body, farm_id=farm.id, context={"days_since": days_since},
    )

    user = db.get(User, farm.user_id)
    if user and user.email:
        body_html = (
            f"<p>We haven't received a usable satellite pass for <b>{name_esc}</b> in "
            f"<b>{days_since} days</b>. This can happen from persistent cloud cover or a gap "
            f"in satellite revisits -- the dashboard is showing your last known reading in "
            f"the meantime.</p>"
        )
        sent = _send_email_best_effort(
            user.email, subject,
            render_email(preheader=body, heading="No new imagery in a while", body_html=body_html,
                         cta_label="Open Vyom Engine", cta_url=dashboard_url),
            text_fallback=f"{body}\n\n{dashboard_url}",
        )
        if sent:
            _mark_emailed(db, row)


def notify_contact_status(db: Session, user_id, name: str) -> None:
    """In-panel only, no email -- confirms a logged-in user's own Contact Us
    submission went through. Anonymous (pre-login) submissions have no
    user_id to attach this to; the caller (contact.py) skips this entirely
    for those, it does not call this function with a placeholder id."""
    _create(
        db, user_id=user_id, type_="contact_status",
        title="Support message sent",
        body="We've received your message and will get back to you soon.",
    )


def notify_admin_alert(db: Session, source: str, message: str, context: dict | None = None) -> None:
    """Fired from error_log.log_error() for level="error" entries only
    (log_error decides that, not this function). Throttled per `source` --
    a burst of the same recurring error only alerts once per
    settings.admin_alert_throttle_minutes, not once per occurrence.
    In-app notifications go to every user with role='admin'; the email
    goes to the fixed settings.admin_alert_email, deliberately not to each
    admin's own account address."""
    cutoff = datetime.now(timezone.utc) - \
        timedelta(minutes=settings.admin_alert_throttle_minutes)
    recent = db.execute(
        select(Notification.id).where(
            Notification.type == "admin_alert",
            Notification.context["source"].astext == source,
            Notification.created_at >= cutoff,
        ).limit(1)
    ).scalar_one_or_none()
    if recent:
        return  # already alerted for this source recently -- throttled

    admins = db.execute(select(User).where(
        User.role == "admin")).scalars().all()
    title = f"Error in {source}"
    # Panel/DB body is a short preview only -- the full message is still in
    # the email and in errors.html; a 500-char raw exception dump doesn't
    # belong in a small notification card even with word-wrap fixed.
    body = message[:160] + ("..." if len(message) > 160 else "")
    for admin in admins:
        _create(db, user_id=admin.id, type_="admin_alert", title=title, body=body,
                context={"source": source, **(context or {})})

    source_esc = html.escape(source)
    message_esc = html.escape(message[:2000]).replace("\n", "<br>")
    body_html = (
        f"<p><b>Source:</b> {source_esc}</p>"
        f"<p><b>Message:</b><br>{message_esc}</p>"
    )
    if context:
        context_esc = html.escape(str(context)[:1000])
        body_html += f"<p><b>Context:</b><br><code style=\"font-size:12px;\">{context_esc}</code></p>"

    sent = _send_email_best_effort(
        settings.admin_alert_email,
        f"Vyom Engine error: {source}",
        render_email(preheader=message[:150], heading="System error alert", body_html=body_html,
                     cta_label="Open Errors panel", cta_url=f"{settings.dashboard_base_url}/errors.html"),
        text_fallback=f"Source: {source}\n\n{message}",
    )
    if sent:
        rows = db.execute(select(Notification).where(
            Notification.type == "admin_alert",
            Notification.context["source"].astext == source,
            Notification.created_at >= cutoff,
        )).scalars().all()
        for row in rows:
            row.email_sent = True
            db.add(row)
        db.commit()

"""
email_utils -- shared SMTP sending + a themed HTML email template used by
every outbound email in this app: the Contact Us form (vyom/api/contact.py)
and every notification type that emails (vyom/notifications.py).

One send function and one template exist here instead of duplicated inline
HTML in each caller, so every email looks consistent and a theme/branding
change only has to happen in one place.
"""
import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from vyom.config import settings

logger = logging.getLogger("vyom.email_utils")

_LOGO_URL = "https://apiv2.agridoot.co.in:12443/img/app_img//AgriDoot_-_Logo_3_ed8bc3.png"
_CANOPY = "#4c9a5b"
_CANOPY_DARK = "#3d7d49"


class EmailSendError(Exception):
    """Raised when SMTP isn't configured or sending fails -- callers decide
    whether that should surface to a user (contact.py does) or just get
    logged (notifications.py's email sends are best-effort, see there)."""


def send_email(to, subject: str, html_body: str, text_fallback: str,
               attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    """to: a single address or a list of addresses. attachments: optional
    list of (filename, content_bytes, mime_type) tuples -- used by
    vyom/invoicing.py to attach invoice PDFs. Raises EmailSendError on any
    failure (missing config or an SMTP error) -- callers decide how to
    handle that."""
    if not settings.smtp_username or not settings.smtp_app_password:
        raise EmailSendError(
            "SMTP is not configured on the server (missing SMTP_USERNAME/SMTP_APP_PASSWORD in .env)")

    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        raise EmailSendError("No recipient address given")

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = f"Vyom Engine <{settings.smtp_username}>"
    msg["To"] = ", ".join(recipients)

    body = MIMEMultipart("alternative")
    body.attach(MIMEText(text_fallback, "plain", "utf-8"))
    body.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(body)

    for filename, content, mime_type in attachments or []:
        maintype, _, subtype = mime_type.partition("/")
        part = MIMEApplication(content, _subtype=subtype or "octet-stream")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            server.starttls()
            server.login(settings.smtp_username, settings.smtp_app_password)
            server.sendmail(settings.smtp_username,
                            recipients, msg.as_string())
    except smtplib.SMTPException as exc:
        raise EmailSendError(str(exc)) from exc


def render_email(*, preheader: str, heading: str, body_html: str,
                 cta_label: str | None = None, cta_url: str | None = None) -> str:
    """Wraps body_html (already-safe HTML -- callers are responsible for
    escaping any user-supplied text before passing it in here) in the
    AgriDoot/Vyom Engine themed shell: canopy-green header with the real
    AgriDoot logo, a white content card, and a footer linking to
    agridoot.com. preheader is the short hidden preview text most email
    clients show next to the subject line in an inbox list."""
    cta_html = ""
    if cta_label and cta_url:
        cta_html = f"""
        <tr>
          <td style="padding: 8px 0 4px;">
            <a href="{cta_url}"
               style="display:inline-block;background:{_CANOPY};color:#ffffff;
                      text-decoration:none;font-weight:600;font-size:14px;
                      padding:12px 22px;border-radius:8px;">
              {cta_label}
            </a>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#eef3ee;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <div style="display:none;max-height:0;overflow:hidden;">{preheader}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef3ee;padding:32px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="560" cellpadding="0" cellspacing="0"
               style="max-width:560px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.06);">
          <tr>
            <td style="background:linear-gradient(135deg,{_CANOPY},{_CANOPY_DARK});padding:22px 28px;">
              <table role="presentation" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="padding-right:10px;">
                    <img src="{_LOGO_URL}" alt="AgriDoot" width="36" height="36"
                         style="display:block;border-radius:8px;background:#ffffff;">
                  </td>
                  <td>
                    <div style="color:#ffffff;font-size:17px;font-weight:700;">Vyom Engine</div>
                    <div style="color:rgba(255,255,255,0.85);font-size:11px;letter-spacing:0.05em;text-transform:uppercase;">by AgriDoot &middot; Earth Observatory</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:30px 28px 10px;">
              <h1 style="margin:0 0 14px;font-size:19px;color:#1a231d;">{heading}</h1>
              <table role="presentation" cellpadding="0" cellspacing="0" style="font-size:14px;color:#3a423a;line-height:1.6;">
                <tr><td>{body_html}</td></tr>
                {cta_html}
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:22px 28px 26px;border-top:1px solid #eef1ee;margin-top:10px;">
              <p style="margin:14px 0 0;font-size:11.5px;color:#9aa39a;">
                Vyom Engine &middot; <a href="https://agridoot.com" style="color:{_CANOPY};text-decoration:none;">agridoot.com</a>
                &middot; This is an automated message, please don't reply directly to this email.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

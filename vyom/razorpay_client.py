"""razorpay_client -- thin wrapper around Razorpay's REST API, using the
`requests` library already in requirements.txt rather than adding the
`razorpay` SDK as a new dependency. Razorpay's API is small enough (create
an Order, verify a payment/webhook signature) that a raw HTTP client is
simpler to audit than pulling in a whole SDK for it.

Auth: Razorpay's REST API uses HTTP Basic Auth with (key_id, key_secret) --
see https://razorpay.com/docs/api/authentication/. key_secret NEVER leaves
this module; only key_id (settings.razorpay_key_id) is ever handed to the
frontend, which is how Razorpay Checkout.js is designed to be used.
"""
import hashlib
import hmac
import logging

import requests

from vyom.config import settings

logger = logging.getLogger("vyom.razorpay_client")

_BASE_URL = "https://api.razorpay.com/v1"


class RazorpayError(Exception):
    """Raised on any non-2xx response from Razorpay, or missing config."""


def _auth() -> tuple[str, str]:
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise RazorpayError(
            "Razorpay is not configured on the server (missing "
            "RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET in .env)")
    return (settings.razorpay_key_id, settings.razorpay_key_secret)


def create_order(*, amount_paise: int, receipt: str, notes: dict | None = None) -> dict:
    """Creates a Razorpay Order and returns Razorpay's JSON response (has
    'id', 'amount', 'currency', 'status', etc). `receipt` should be a stable
    reference to the row on our side (e.g. a business_subscriptions.id or a
    future farm_plans.id) so a support ticket can always trace an order back
    to what it was for.
    """
    try:
        resp = requests.post(
            f"{_BASE_URL}/orders",
            auth=_auth(),
            json={
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt,
                "notes": notes or {},
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RazorpayError(f"Could not reach Razorpay: {exc}") from exc

    if resp.status_code >= 300:
        logger.error("Razorpay order creation failed: %s %s",
                     resp.status_code, resp.text)
        raise RazorpayError(f"Razorpay order creation failed: {resp.text}")
    return resp.json()


def verify_payment_signature(*, order_id: str, payment_id: str, signature: str) -> bool:
    """Verifies the signature Razorpay Checkout returns to the FRONTEND on
    success (razorpay_order_id, razorpay_payment_id, razorpay_signature).
    This is a fast client-side-confirmation check ONLY -- it is spoofable by
    a modified frontend (someone could POST fabricated values), so it must
    never be the sole source of truth for granting access. The webhook
    handler (verify_webhook_signature below), which Razorpay calls
    server-to-server, is the authoritative confirmation. Use this function
    to give the user immediate UI feedback; flip business_status/etc. only
    once the webhook has also confirmed it (see vyom/api/billing.py).
    """
    _, key_secret = _auth()
    payload = f"{order_id}|{payment_id}".encode()
    expected = hmac.new(key_secret.encode(), payload,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(*, raw_body: bytes, signature: str) -> bool:
    """Verifies X-Razorpay-Signature on an incoming webhook POST, using the
    SEPARATE webhook secret configured when the webhook URL was registered
    in the Razorpay dashboard (not razorpay_key_secret). raw_body must be
    the exact, unparsed request bytes -- re-serializing parsed JSON can
    produce a different byte sequence (key ordering, whitespace) and break
    the signature check.
    """
    if not settings.razorpay_webhook_secret:
        raise RazorpayError(
            "RAZORPAY_WEBHOOK_SECRET is not configured on the server")
    expected = hmac.new(settings.razorpay_webhook_secret.encode(),
                        raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

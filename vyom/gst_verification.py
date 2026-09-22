"""gst_verification -- verifies a GSTIN and fetches the registered company
name/address behind it, for the business-account upgrade flow (point 6 of
the monetization spec). Defaults to gstinapi.in (https://www.gstinapi.in),
a third-party GST Suvidha Provider (GSP) network client with a free tier
(100 lookups/month, no card required) -- reasonable for the low volume a
business-signup verification step needs.

This is deliberately isolated behind one small function so swapping to a
different provider (Cashfree, Deepvue, etc. all offer similar GSTIN lookup
APIs) later is a one-file change -- nothing outside this module should know
which provider is behind GstinLookupResult.

NOT the same as bank-grade KYC/AML verification -- this confirms the GSTIN
exists, is currently Active on government GST records, and returns the
legal name/address on file. It does not confirm the PERSON upgrading the
account is authorized to act for that company; that's a reasonable
practical limit for a self-serve SaaS signup, not a compliance guarantee.
"""
import logging
import re

import requests

from vyom.config import settings

logger = logging.getLogger("vyom.gst_verification")

# 2 digits (state code) + 10-char PAN + 1 digit (entity code) + 1 char (Z,
# fixed) + 1 alphanumeric checksum -- rejects an obviously malformed input
# before spending an API credit on it.
_GSTIN_PATTERN = re.compile(
    r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z][Z][0-9A-Z]$")


class GstVerificationError(Exception):
    """Message is safe to show directly to the end user."""


class GstinLookupResult:
    def __init__(self, gstin: str, legal_name: str, address: str | None, status: str):
        self.gstin = gstin
        self.legal_name = legal_name
        self.address = address
        self.status = status  # "Active", "Cancelled", etc.


def verify_gstin(gstin: str) -> GstinLookupResult:
    gstin = gstin.strip().upper()
    if not _GSTIN_PATTERN.match(gstin):
        raise GstVerificationError(
            "That doesn't look like a valid 15-character GSTIN. Please check and try again.")

    if not settings.gst_verification_api_key:
        raise GstVerificationError(
            "GST verification is not configured on the server (missing GST_VERIFICATION_API_KEY).")

    try:
        resp = requests.get(
            f"{settings.gst_verification_base_url}/gstin/{gstin}",
            headers={"x-api-key": settings.gst_verification_api_key},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise GstVerificationError(
            f"Could not reach the GST verification service right now. Please try again shortly.") from exc

    if resp.status_code == 404:
        raise GstVerificationError(
            "No business is registered under that GSTIN. Please double-check the number.")
    if resp.status_code == 400:
        raise GstVerificationError(
            "That GSTIN failed validation. Please double-check the number.")
    if resp.status_code >= 500 or resp.status_code == 502:
        raise GstVerificationError(
            "The GST verification service is temporarily unavailable. Please try again shortly.")
    if resp.status_code >= 300:
        logger.error("GST verification lookup failed: %s %s",
                     resp.status_code, resp.text)
        raise GstVerificationError(
            "Could not verify that GSTIN right now. Please try again shortly.")

    envelope = resp.json()
    data = envelope.get("data") or {}
    status = data.get("status", "Unknown")
    if status != "Active":
        raise GstVerificationError(
            f"This GSTIN's registration status is '{status}', not Active. "
            "Only businesses with an active GST registration can be verified.")

    legal_name = data.get("legal_name")
    if not legal_name:
        raise GstVerificationError(
            "The GST verification service did not return a registered business name for this GSTIN.")

    return GstinLookupResult(
        gstin=gstin, legal_name=legal_name, address=data.get("address"), status=status,
    )

"""gst -- single place every priced flow computes GST, so the rate and the
rounding rule only ever live in one spot. Every price quoted elsewhere in
this app (config.py, farm plans, business subscription) is GST-EXCLUSIVE;
this module is what turns a base amount into what Razorpay actually charges.

Rounding: GST amounts are rounded to the nearest paise (round-half-up) --
good enough for now. If NovosEdge's accountant wants a different rounding
convention (e.g. always round up) for invoice-compliance reasons, change it
here only.
"""
from decimal import Decimal, ROUND_HALF_UP

from vyom.config import settings


def gst_paise_for(base_paise: int) -> int:
    """GST amount (in paise) on a GST-exclusive base amount."""
    gst = (Decimal(base_paise) * Decimal(str(settings.gst_percent)) / Decimal(100)
           ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(gst)


def total_with_gst(base_paise: int) -> tuple[int, int, int]:
    """Returns (base_paise, gst_paise, total_paise) -- the exact breakdown
    every invoice/receipt line should display, never just a single total."""
    gst_paise = gst_paise_for(base_paise)
    return base_paise, gst_paise, base_paise + gst_paise

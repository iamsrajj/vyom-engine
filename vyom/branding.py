"""branding -- the one local copy of the AgriDoot logo, shared by every
server-side surface that needs its raw bytes: PDF invoices
(vyom/invoice_pdf.py) and HTML emails (vyom/email_utils.py). Neither one
hot-links apiv2.agridoot.co.in anymore -- both read the same local file the
web dashboard itself uses (see scripts/download_assets.py / .sh / .ps1,
which populate web/assets/img/). The dashboard's own pages reference that
file directly at /assets/img/agridoot-logo.png; this module exists only for
surfaces that need the raw bytes server-side (embedding in a PDF, or as a
MIME inline image in an email) rather than a URL a browser can fetch.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LOGO_PATH = Path(__file__).resolve().parent.parent / \
    "web" / "assets" / "img" / "agridoot-logo.png"

# Read once per process and cached -- invoices/emails can be generated
# repeatedly, and there's no reason to re-read the same file from disk every
# single time. None means "not read yet"; False means "missing, don't retry
# this process" (a logo is cosmetic -- worth failing quietly rather than
# breaking an invoice or email send if the download script was never run).
_logo_cache: bytes | None | bool = None


def read_logo_bytes() -> bytes | None:
    global _logo_cache
    if _logo_cache is None:
        try:
            _logo_cache = LOGO_PATH.read_bytes()
        except OSError:
            logger.warning(
                "Logo not found at %s -- run scripts/download_assets.py "
                "(or the .sh/.ps1 equivalent) to fetch it. PDFs and emails "
                "will render without a logo until then.", LOGO_PATH)
            _logo_cache = False
    return _logo_cache or None

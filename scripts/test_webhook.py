"""test_webhook.py -- fires a correctly-signed fake Razorpay webhook event
at your own /billing/webhooks/razorpay endpoint, so you can verify signature
verification and routing are working without needing Razorpay's dashboard
test-webhook feature or a real payment.

Usage:
    Run it from your vyom-engine directory so it can read .env directly --
    no need to paste the secret into this file at all:
        python3 test_webhook.py
    Or, if your shell already has it exported some other way:
        RAZORPAY_WEBHOOK_SECRET=xxxx python3 test_webhook.py

Expected results:
    HTTP 200 {"status":"ignored"}       -> working correctly. "ignored" is
                                            expected since order_test_fake123
                                            doesn't exist in business_subscriptions.
    HTTP 400 {"detail":"Invalid signature"} -> the secret this script picked up
                                            doesn't match what's configured in
                                            the Razorpay dashboard (check for
                                            trailing spaces/quotes in .env,
                                            and that the app was restarted
                                            after editing .env).
    HTTP 500                            -> RAZORPAY_WEBHOOK_SECRET isn't set
                                            on the server at all.
"""
import hmac
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

URL = "https://vyom.agridoot.in/billing/webhooks/razorpay"


def _load_secret() -> str:
    """Reads RAZORPAY_WEBHOOK_SECRET the same way the app does: real
    environment variable first (covers systemd Environment= / exported
    shell vars), then falls back to parsing a .env file in the current
    directory (covers the common case of running this script from
    ~/P/vyom-engine where the app's own .env lives) -- no python-dotenv
    dependency needed for a one-off script like this."""
    value = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if value:
        return value.strip()

    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("RAZORPAY_WEBHOOK_SECRET="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")

    print("ERROR: RAZORPAY_WEBHOOK_SECRET not found in the environment or "
          "in a .env file in the current directory.\n"
          "Run this from your vyom-engine directory (where .env lives), or "
          "set it explicitly: RAZORPAY_WEBHOOK_SECRET=xxxx python3 test_webhook.py")
    sys.exit(1)


SECRET = _load_secret()

body = json.dumps({
    "event": "payment.captured",
    "payload": {"payment": {"entity": {
        "id": "pay_test_fake123",
        "order_id": "order_test_fake123",
    }}},
}, separators=(",", ":"))

signature = hmac.new(SECRET.encode(), body.encode(),
                     hashlib.sha256).hexdigest()

result = subprocess.run([
    "curl", "-i", URL,
    "-X", "POST",
    "-H", "Content-Type: application/json",
    "-H", f"X-Razorpay-Signature: {signature}",
    "-d", body,
], capture_output=True, text=True)

print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)

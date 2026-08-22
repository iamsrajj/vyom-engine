"""
auth_broker — acquires and caches Copernicus Data Space Ecosystem (CDSE) OAuth2
access tokens, so no other module ever talks to the identity server directly.

Supports two grant types:
  - client_credentials  (preferred: CDSE_CLIENT_ID + CDSE_CLIENT_SECRET)
  - password grant       (fallback: CDSE_USERNAME + CDSE_PASSWORD, client_id "cdse-public")

CDSE access tokens are short-lived (~10 min). This broker refreshes proactively,
30 seconds before expiry, so downstream calls never hit a 401 mid-request.
"""
import time
import threading
import logging

import requests

from vyom.config import settings

logger = logging.getLogger("vyom.auth_broker")

_REFRESH_MARGIN_SECONDS = 30


class AuthBroker:
    def __init__(self):
        self._token = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        """Return a valid access token, refreshing it first if it's expired or about to be."""
        with self._lock:
            if self._token is None or time.time() >= self._expires_at - _REFRESH_MARGIN_SECONDS:
                self._refresh()
            return self._token

    def _refresh(self):
        if settings.cdse_client_id and settings.cdse_client_secret:
            data = {
                "grant_type": "client_credentials",
                "client_id": settings.cdse_client_id,
                "client_secret": settings.cdse_client_secret,
            }
        elif settings.cdse_username and settings.cdse_password:
            data = {
                "grant_type": "password",
                "client_id": "cdse-public",
                "username": settings.cdse_username,
                "password": settings.cdse_password,
            }
        else:
            raise RuntimeError(
                "No Copernicus credentials configured. Set CDSE_CLIENT_ID/CDSE_CLIENT_SECRET "
                "or CDSE_USERNAME/CDSE_PASSWORD in .env"
            )

        resp = requests.post(settings.cdse_token_url, data=data, timeout=30)
        resp.raise_for_status()
        payload = resp.json()

        self._token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 600))
        logger.info("Refreshed CDSE access token, valid for %ss", payload.get("expires_in"))

    def auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self.get_token()}"}


# Module-level singleton — every service imports this instance rather than
# instantiating its own broker, so token refresh happens in one place.
auth_broker = AuthBroker()

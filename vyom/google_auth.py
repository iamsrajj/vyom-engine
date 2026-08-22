"""
google_auth -- verifies the ID token Google Identity Services hands the
frontend after "Continue with Google". This is a signature + audience +
issuer check against Google's own public keys (via the google-auth library),
not a trust-the-client parse of the JWT payload -- a client could otherwise
hand us any fabricated email/name/sub it wanted.
"""
from dataclasses import dataclass

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from vyom.config import settings


@dataclass
class GoogleIdentity:
    sub: str
    email: str
    name: str
    picture: str | None


_request = google_requests.Request()


def verify_google_id_token(token: str) -> GoogleIdentity:
    """Raises ValueError (via google-auth) if the token is invalid, expired,
    or issued for a different Google OAuth Client ID than ours."""
    claims = id_token.verify_oauth2_token(
        token, _request, settings.google_client_id)
    return GoogleIdentity(
        sub=claims["sub"],
        email=claims["email"],
        name=claims.get("name", claims["email"]),
        picture=claims.get("picture"),
    )

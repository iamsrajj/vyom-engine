"""
Quick standalone check: confirms your .env credentials can actually get a
Copernicus access token, before you bother starting the full API/Celery stack.

Run from the vyom-engine root:
    python3 check_cdse_auth.py
"""
from vyom.auth_broker import auth_broker

if __name__ == "__main__":
    try:
        token = auth_broker.get_token()
        print("Auth OK — got a token starting with:", token[:20] + "...")
    except Exception as exc:
        print("Auth FAILED:", exc)
        print("Check CDSE_CLIENT_ID / CDSE_CLIENT_SECRET (or username/password) in .env")

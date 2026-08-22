"""
test_wasabi.py -- confirms the Wasabi credentials in .env actually work
(list both buckets, write, delete). Safe to commit to git: no real
credentials live in this file, only the *names* of the env vars.

Usage:
    python test_wasabi.py

Reads the same env var names vyom/config.py uses, so this stays a true
end-to-end check of your real .env, not a separate hardcoded config.
"""
import os
import sys

import boto3

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads .env from the current directory if present
except ImportError:
    print("(python-dotenv not installed -- reading from real environment "
          "variables only. `pip install python-dotenv` to also load a "
          "local .env file automatically.)")

REQUIRED_VARS = [
    "S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY",
    "S3_REGION", "S3_BUCKET_RAW", "S3_BUCKET_PROCESSED",
]

missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
if missing:
    print(f"Missing env vars: {', '.join(missing)}")
    print("Set them in your .env (see .env.production.example) or export "
          "them directly before running this script.")
    sys.exit(1)

client = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT_URL"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name=os.environ["S3_REGION"],
)

bucket_raw = os.environ["S3_BUCKET_RAW"]
bucket_processed = os.environ["S3_BUCKET_PROCESSED"]

print("Buckets found:")
for b in client.list_buckets()["Buckets"]:
    print(" -", b["Name"])

client.put_object(Bucket=bucket_raw, Key="test.txt", Body=b"hello wasabi")
print(f"Write test OK ({bucket_raw})")

client.delete_object(Bucket=bucket_raw, Key="test.txt")
print(f"Cleanup OK ({bucket_raw})")

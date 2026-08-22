"""
Central configuration for Vyom Engine (Phase 1 slice).
All values are loaded from environment variables / .env so nothing is hardcoded.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Copernicus Data Space Ecosystem
    cdse_client_id: str = ""
    cdse_client_secret: str = ""
    cdse_username: str = ""
    cdse_password: str = ""

    cdse_token_url: str = (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    )
    cdse_odata_url: str = "https://catalogue.dataspace.copernicus.eu/odata/v1"
    cdse_download_url: str = "https://download.dataspace.copernicus.eu/odata/v1"

    # Database
    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/vyom"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Storage backend: "local" (disk on this server) or "s3" (MinIO / any S3-compatible
    # object store). "s3" is what lets you run multiple worker machines that all see
    # the same raw/processed files, instead of being pinned to one box's disk.
    storage_backend: str = "local"
    raw_data_dir: str = "./data/raw"
    processed_data_dir: str = "./data/processed"

    # MinIO default; use AWS endpoint for real S3
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket_raw: str = "vyom-raw"
    s3_bucket_processed: str = "vyom-processed"
    s3_region: str = "us-east-1"
    s3_use_ssl: bool = False

    # Discovery defaults — Sentinel-2 (optical)
    default_max_cloud_cover: float = 40.0
    s2_collection: str = "SENTINEL-2"
    s2_product_type: str = "S2MSI2A"

    # Discovery defaults — Sentinel-1 (SAR, cloud-independent — fills gaps during monsoon)
    s1_collection: str = "SENTINEL-1"
    s1_product_type: str = "IW_GRDH_1S"

    # Indices computed per platform. Adding a new index later is: implement the
    # formula in processing/indices.py or processing/sar_indices.py, add its name
    # here — no schema migration needed since indices are stored as JSONB.
    s2_indices: list[str] = ["NDVI", "NDWI",
                             "NDMI", "MSAVI2", "NDRE", "SOC_VIS"]
    s1_indices: list[str] = ["RVI", "VV_VH_RATIO"]

    # Beta login gate. AUTH_USERS is "username:password,username2:password2" --
    # see vyom/auth.py docstring for the honest limitation of this approach.
    auth_secret_key: str = "change-this-to-a-long-random-string"
    auth_session_ttl_hours: int = 24
    auth_users: str = ""

    # Comma-separated usernames (matching entries in auth_users) allowed to see
    # the Errors panel. Empty = every logged-in beta user can see it (fine for
    # a small trusted beta list; tighten once real user roles exist -- see the
    # auth/signup rebuild, which will replace this with a proper role check).
    error_panel_usernames: str = ""


settings = Settings()

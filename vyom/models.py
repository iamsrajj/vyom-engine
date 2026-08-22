import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    Column, String, Numeric, DateTime, Date, Text, ForeignKey, BigInteger, Integer, UniqueConstraint, Boolean
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from vyom.db import Base


class CatalogProduct(Base):
    __tablename__ = "catalog_products"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # "S2" (optical) or "S1" (SAR)
    platform = Column(String, nullable=False, default="S2")
    collection = Column(String, nullable=False, default="SENTINEL-2")
    product_id = Column(String, unique=True, nullable=False)
    product_name = Column(String, nullable=False)
    tile_id = Column(String)
    acquisition_date = Column(DateTime(timezone=True), nullable=False)
    cloud_cover = Column(Numeric)  # null/meaningless for S1
    footprint = Column(
        Geometry(geometry_type="POLYGON", srid=4326), nullable=False)
    status = Column(String, nullable=False, default="discovered")
    raw_path = Column(Text)

    # One row per index -> COG path, e.g. {"NDVI": "s3://.../NDVI.tif", "NDWI": "..."}.
    # JSONB rather than one DB column per index means adding a new index later is a
    # code change only -- no migration required.
    processed_indices = Column(JSONB, nullable=False, server_default="{}")

    checksum = Column(Text)
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True),
                        default=datetime.utcnow, onupdate=datetime.utcnow)


class Polygon(Base):
    __tablename__ = "polygons"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), nullable=False,
                       default=uuid.UUID(int=0))
    user_id = Column(UUID(as_uuid=True), nullable=False)
    name = Column(String)
    geom = Column(Geometry(geometry_type="POLYGON", srid=4326), nullable=False)
    area_ha = Column(Numeric)
    crop_type = Column(String)
    # populated at creation from reverse geocoding or client-supplied
    country = Column(String)
    # crop age is always derived as (today - sowing_date), never stored
    sowing_date = Column(Date)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class PolygonTileMap(Base):
    __tablename__ = "polygon_tile_map"

    polygon_id = Column(UUID(as_uuid=True), ForeignKey(
        "polygons.id", ondelete="CASCADE"), primary_key=True)
    product_id = Column(UUID(as_uuid=True), ForeignKey(
        "catalog_products.id", ondelete="CASCADE"), primary_key=True)


class ZonalStat(Base):
    __tablename__ = "zonal_stats"
    __table_args__ = (
        UniqueConstraint("polygon_id", "product_id", "metric",
                         name="uq_zstats_polygon_product_metric"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    polygon_id = Column(UUID(as_uuid=True), ForeignKey(
        "polygons.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey(
        "catalog_products.id", ondelete="CASCADE"), nullable=False)
    acquisition_date = Column(DateTime(timezone=True), nullable=False)
    metric = Column(String, nullable=False)  # e.g. 'NDVI_mean', 'RVI_std'
    value = Column(Numeric)
    pixel_count = Column(Integer)
    cloud_pct = Column(Numeric)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class ErrorLog(Base):
    """Single place every failure across every task/module/API route gets
    written to, so the dashboard's Errors panel is one query instead of
    grepping journalctl across discovery/download/process/zonal-stats/API.
    Deliberately flat (no FK constraints on farm_id/product_id) since a
    logging path must never itself fail because a farm was since deleted."""
    __tablename__ = "error_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    # e.g. "tasks.refresh_farm", "download_manager", "pipeline_s1",
    # "pipeline_s2", "zonal_stats", "discovery", "api.farms", "auth"
    source = Column(String, nullable=False)
    # e.g. "S2", "S1", or null for non-platform sources (API/auth)
    platform = Column(String)
    level = Column(String, nullable=False, default="error")  # error | warning
    message = Column(Text, nullable=False)
    traceback = Column(Text)
    # free-form context: farm_id, product_id, product_name, request path, etc.
    context = Column(JSONB, nullable=False, server_default="{}")
    resolved = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class User(Base):
    """A real account -- replaces the flat AUTH_USERS env-var list. Every
    account starts with Google sign-in (name/email/picture come from Google's
    verified ID token, never trusted from the client directly) then completes
    a profile (org/designation/address/phone) with the phone verified by OTP
    before the account is usable."""
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Short human-friendly identifier shown in the UI/support conversations,
    # e.g. "AGD-7F3K2Q" -- distinct from the internal uuid `id`.
    account_id = Column(String, unique=True, nullable=False)

    email = Column(String, unique=True, nullable=False)
    # Google's stable user id
    google_sub = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    profile_img_url = Column(Text)

    organization = Column(String, nullable=False)
    designation = Column(String, nullable=False)
    address = Column(Text, nullable=False)

    phone_cc = Column(String, nullable=False, default="91")
    phone = Column(String, unique=True, nullable=False)
    phone_verified = Column(Boolean, nullable=False, server_default="false")

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True),
                        default=datetime.utcnow, onupdate=datetime.utcnow)


class OtpVerification(Base):
    """One row per OTP attempt. The actual OTP digits are never sent to or
    trusted from the client -- AgriDoot's genotp API returns otp_value to
    THIS backend, which stores a salted hash of it here and compares against
    what the user types in, server-side. See vyom/otp_client.py."""
    __tablename__ = "otp_verifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_cc = Column(String, nullable=False)
    phone = Column(String, nullable=False, index=True)
    # "signup" (new account, phone not yet in users table) or
    # "signin" (phone must already belong to an existing user)
    purpose = Column(String, nullable=False)

    # sha256(otp_value), never plaintext
    otp_hash = Column(String, nullable=False)
    # AgriDoot genotp's own otp_id, for support/debugging
    provider_otp_id = Column(String)
    provider_request_id = Column(String)  # AgriDoot genotp's own request_id

    attempts = Column(Integer, nullable=False, server_default="0")
    max_attempts = Column(Integer, nullable=False, server_default="5")
    verified = Column(Boolean, nullable=False, server_default="false")
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

"""api/business_api_credentials -- lets a logged-in business user generate,
list, and revoke their own partner-API credentials. Authenticated with the
normal dashboard session (require_auth), NOT the API key/secret scheme --
that's for the partner API itself (vyom/api_auth.py, vyom/api/partner_farms.py).
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.auth import require_auth, stable_owner_uuid
from vyom.db import get_db
from vyom.models import ApiCredential, User
from vyom.api_auth import generate_credential

router = APIRouter(prefix="/business/api-credentials", tags=["business"])


def _get_business_user(db: Session, current_user: str) -> User:
    user = db.get(User, stable_owner_uuid(current_user))
    if user is None:
        raise HTTPException(
            400, "Full account required (Google or phone sign-in).")
    if user.account_type != "business" or user.business_status != "active":
        raise HTTPException(
            403, "An active business account is required to manage API credentials. "
            "Upgrade via /billing/business/upgrade first.")
    return user


class GenerateCredentialRequest(BaseModel):
    label: Optional[str] = None


class NewCredentialOut(BaseModel):
    id: UUID
    api_key: str
    api_secret: str  # shown ONLY in this one response, never again
    label: Optional[str]
    created_at: datetime


class CredentialOut(BaseModel):
    id: UUID
    api_key: str
    secret_last4: str
    label: Optional[str]
    status: str
    created_at: datetime
    last_used_at: Optional[datetime]

    class Config:
        from_attributes = True


@router.post("", response_model=NewCredentialOut)
def create_credential(payload: GenerateCredentialRequest, current_user: str = Depends(require_auth),
                      db: Session = Depends(get_db)):
    user = _get_business_user(db, current_user)
    credential, raw_secret = generate_credential(
        db, user=user, label=payload.label)
    return NewCredentialOut(
        id=credential.id, api_key=credential.api_key, api_secret=raw_secret,
        label=credential.label, created_at=credential.created_at,
    )


@router.get("", response_model=list[CredentialOut])
def list_credentials(current_user: str = Depends(require_auth), db: Session = Depends(get_db)):
    user = _get_business_user(db, current_user)
    return list(db.execute(
        select(ApiCredential).where(ApiCredential.user_id == user.id)
        .order_by(ApiCredential.created_at.desc())
    ).scalars())


@router.post("/{credential_id}/revoke", response_model=CredentialOut)
def revoke_credential(credential_id: UUID, current_user: str = Depends(require_auth),
                      db: Session = Depends(get_db)):
    user = _get_business_user(db, current_user)
    credential = db.get(ApiCredential, credential_id)
    if credential is None or credential.user_id != user.id:
        raise HTTPException(404, "Credential not found")
    if credential.status == "active":
        credential.status = "revoked"
        credential.revoked_at = datetime.utcnow()
        credential.revoked_reason = "revoked_by_user"
        db.commit()
        db.refresh(credential)
    return credential

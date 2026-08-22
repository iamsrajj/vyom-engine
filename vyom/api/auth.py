from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from vyom.auth import verify_credentials, issue_token

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(payload: LoginRequest):
    if not verify_credentials(payload.username, payload.password):
        raise HTTPException(401, "Invalid username or password")
    token, expires_at = issue_token(payload.username)
    return {"token": token, "expires_at": expires_at, "username": payload.username}

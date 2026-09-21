import logging

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from vyom.api import farms, tiles, auth as auth_api, errors as errors_api, prewarm as prewarm_api, reference as reference_api, contact as contact_api, notifications as notifications_api
from vyom.api import billing as billing_api
from vyom.api import business_api_credentials, partner_farms, developer_docs
from vyom.api import business_onboarding
from vyom.api_auth import ApiV1Error
from vyom.auth import require_auth, require_auth_query
from vyom.config import settings
from vyom.error_log import log_error

logger = logging.getLogger("vyom.api")

# Security fix: refuse to start if the JWT signing secret is still the
# well-known placeholder from config.py's default. That default is
# documented in .env.production.example as something to replace -- this is
# the code-level backstop for when that documentation gets missed (a
# skipped .env line, a rushed deploy). If this fires, generate a real
# secret: `openssl rand -hex 32`, set AUTH_SECRET_KEY in .env, restart.
_INSECURE_DEFAULT_SECRET = "change-this-to-a-long-random-string"
if settings.auth_secret_key == _INSECURE_DEFAULT_SECRET:
    raise RuntimeError(
        "AUTH_SECRET_KEY is still set to its insecure default. Anyone who "
        "knows this default value (it's public, in the source code) could "
        "forge a valid session token for any user. Set a real random value "
        "in .env before starting: `openssl rand -hex 32`, then set "
        "AUTH_SECRET_KEY=<that value> and restart."
    )

app = FastAPI(
    title="Vyom Engine - By AgriDoot",
    description="Multi-index Sentinel-1/Sentinel-2 ingestion, processing, and farm web mapping.",
    version="0.2.0",
)

# Security fix: was allow_origins=["*"] (any website could call this API).
# Now driven by settings.cors_allowed_origins (see config.py) -- defaults to
# localhost-only for local dev. Set CORS_ALLOWED_ORIGINS in .env to your
# real frontend domain(s) before serving real traffic.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip()
                   for o in settings.cors_allowed_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_api.router)
# farms endpoints require a Bearer token (see vyom/auth.py); tiles endpoints
# require a ?token= query param instead, since map libraries load tiles as
# plain image requests with no custom headers available.
app.include_router(farms.router, dependencies=[Depends(require_auth)])
# reference.router proxies NovosEdge's crop/soil lists (see that file for
# why this is a server-side proxy, not a direct browser call) -- gated the
# same as farms, since it's only ever called from the logged-in dashboard.
app.include_router(reference_api.router, dependencies=[Depends(require_auth)])
app.include_router(tiles.router, dependencies=[Depends(require_auth_query)])
# errors.router is protected inside errors.py itself (admin-only), not here,
# since it needs a different check than plain require_auth -- see that file.
app.include_router(errors_api.router)
# prewarm.router is protected inside prewarm.py itself (admin-only, same
# gate as errors_api), not here -- see that file.
app.include_router(prewarm_api.router)
# contact.router is deliberately public (no require_auth) -- see that
# file's module docstring for why, and the known spam-risk tradeoff.
app.include_router(contact_api.router)
app.include_router(notifications_api.router,
                   dependencies=[Depends(require_auth)])
# billing.py's three routers each gate individual routes themselves (mixed
# public/authenticated/admin-only within the same file -- e.g. the Razorpay
# webhook must stay public, /coupons/public must stay public, everything
# else requires a session), so none of them get a blanket router-level
# dependency here the way farms/tiles/reference/notifications do above.
app.include_router(billing_api.router)
app.include_router(billing_api.coupons_router)
app.include_router(billing_api.admin_coupons_router)
app.include_router(business_api_credentials.router,
                   dependencies=[Depends(require_auth)])
app.include_router(business_onboarding.router,
                   dependencies=[Depends(require_auth)])
# partner_farms.router is NOT given a require_auth dependency here -- it
# authenticates via API key/secret (require_business_api_auth, called
# per-route inside vyom/api/partner_farms.py itself), a completely
# different scheme from the dashboard session cookie/JWT every other
# router above uses.
app.include_router(partner_farms.router)
# Public docs -- no auth dependency, same reasoning as the router comment
# in developer_docs.py itself.
app.include_router(developer_docs.router)


@app.middleware("http")
async def partner_api_audit_log(request: Request, call_next):
    """Full per-call audit trail for the partner API (point 5 of the
    monetization spec) -- every request under /api/v1/ gets a row in
    api_access_log, whether it succeeded or was rejected at any auth gate.
    require_business_api_auth (vyom/api_auth.py) sets request.state.
    api_credential_id/api_user_id as soon as a credential is identified,
    even if a later gate then rejects the request -- so a revoked-key or
    payment-due REJECTION is exactly as visible here as a successful call.

    A synchronous DB write per request adds latency; acceptable at current
    partner-API volume, flagged as a candidate for batching/async logging
    if that ever changes.
    """
    if not request.url.path.startswith("/api/v1"):
        return await call_next(request)

    import time as _time
    start = _time.monotonic()
    response = await call_next(request)
    duration_ms = int((_time.monotonic() - start) * 1000)

    try:
        from vyom.db import SessionLocal
        from vyom.models import ApiAccessLog
        db = SessionLocal()
        try:
            db.add(ApiAccessLog(
                api_credential_id=getattr(
                    request.state, "api_credential_id", None),
                user_id=getattr(request.state, "api_user_id", None),
                method=request.method, path=request.url.path,
                status_code=response.status_code,
                ip_address=request.client.host if request.client else None,
                duration_ms=duration_ms,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 -- audit logging must never break a real request
        logger.error("Failed to write api_access_log row: %s", exc)

    return response


@app.exception_handler(ApiV1Error)
async def api_v1_error_handler(request: Request, exc: ApiV1Error):
    """Renders the standard {"error": {code, message, retriable}} envelope
    for the business partner API -- kept deliberately separate from the
    dashboard's plain {"detail": ...} HTTPException shape used everywhere
    else in this app, since integrators need a stable, documented error
    contract to branch their own retry/alerting logic on (see the
    monetization spec's §5 reliability notes)."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code,
                           "message": exc.message, "retriable": exc.retriable}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catches anything that escapes a route handler unhandled (a bug, not a
    deliberate HTTPException) and writes it to the same error_logs table the
    Celery pipeline writes to, so API-side failures show up in the same
    dashboard panel instead of only ever being visible in server logs."""
    logger.exception("Unhandled API exception on %s %s",
                     request.method, request.url.path)
    log_error("api", str(exc), context={
              "method": request.method, "path": request.url.path})
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
def health():
    return {"status": "ok"}

import logging

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from vyom.api import farms, tiles, auth as auth_api, errors as errors_api, prewarm as prewarm_api
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
app.include_router(tiles.router, dependencies=[Depends(require_auth_query)])
# errors.router is protected inside errors.py itself (admin-only), not here,
# since it needs a different check than plain require_auth -- see that file.
app.include_router(errors_api.router)
# prewarm.router is protected inside prewarm.py itself (admin-only, same
# gate as errors_api), not here -- see that file.
app.include_router(prewarm_api.router)


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

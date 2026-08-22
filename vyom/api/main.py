import logging

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from vyom.api import farms, tiles, auth as auth_api, errors as errors_api
from vyom.auth import require_auth, require_auth_query
from vyom.error_log import log_error

logger = logging.getLogger("vyom.api")

app = FastAPI(
    title="Vyom Engine - By AgriDoot",
    description="Multi-index Sentinel-1/Sentinel-2 ingestion, processing, and farm web mapping.",
    version="0.2.0",
)

# Permissive CORS for local dev so web/index.html (served from any port/host)
# can call this API directly. Tighten this to your actual dashboard origin
# before anything resembling wider production use.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

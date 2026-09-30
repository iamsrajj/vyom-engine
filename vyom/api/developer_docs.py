"""api/developer_docs -- public documentation for the business partner API.
No auth required to READ the docs (same as Stripe/Twilio/any public API
reference) -- the security boundary is the real API's own X-Api-Key/
X-Api-Secret check, not access to its documentation.
"""
from fastapi import APIRouter, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from vyom.config import settings
from vyom.openapi_partner import build_partner_openapi

router = APIRouter(prefix="/developers", tags=["developer-docs"])


@router.get("/openapi.json", include_in_schema=False)
def partner_openapi_schema(request: Request):
    return JSONResponse(build_partner_openapi(request.app))


@router.get("/docs", include_in_schema=False, response_class=HTMLResponse)
def partner_docs():
    return get_swagger_ui_html(
        openapi_url="/developers/openapi.json",
        title="Vyom Engine Partner API -- Docs",
        # Local asset (see scripts/download_assets.py), same file the
        # dashboard and playground use -- an absolute URL built from
        # DASHBOARD_BASE_URL rather than a relative /assets/... path, since
        # this route's own origin isn't guaranteed to be the same one the
        # static web/ files are served from on every deployment (same
        # reasoning as the absolute tile_url_template in
        # vyom/api/partner_tiles.py).
        swagger_favicon_url=f"{settings.dashboard_base_url}/assets/img/agridoot-logo.png",
        # Hides the auto-generated "Schemas" section at the bottom (every
        # request/response model, including internal ones like CouponIn/
        # ContactRequest that a business integrator never touches directly --
        # they're only listed because *some* partner endpoint references
        # them). The parameter list stays visible in each endpoint anyway,
        # which is what an integrator actually needs. -1 hides the section
        # entirely rather than just collapsing it (0 would still show empty
        # headers for every schema).
        swagger_ui_parameters={"defaultModelsExpandDepth": -1},
    )

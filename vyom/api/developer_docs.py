"""api/developer_docs -- public documentation for the business partner API.
No auth required to READ the docs (same as Stripe/Twilio/any public API
reference) -- the security boundary is the real API's own X-Api-Key/
X-Api-Secret check, not access to its documentation.
"""
from fastapi import APIRouter, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

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
        swagger_favicon_url="https://apiv2.agridoot.co.in:12443/img/app_img//AgriDoot_-_Logo_3_ed8bc3.png",
    )

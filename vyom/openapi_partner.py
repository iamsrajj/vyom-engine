"""openapi_partner -- builds a PUBLIC OpenAPI schema containing ONLY the
partner-api-tagged routes (vyom/api/partner_farms.py), never the
dashboard's internal routes (auth, admin, billing, tiles, etc). This is
what's served at /developers/openapi.json and rendered by Swagger UI at
/developers/docs (vyom/api/developer_docs.py) -- a business integrator
should be able to see this and nothing else about how the app works
internally.
"""
from fastapi.openapi.utils import get_openapi

PARTNER_TAG = "partner-api"


def build_partner_openapi(app) -> dict:
    full = get_openapi(
        title="Vyom Engine Partner API",
        version="1.0.0",
        description=(
            "Create, update, and fetch satellite-index data for farms you manage "
            "through your Vyom Engine business account. Every request needs the "
            "X-Api-Key and X-Api-Secret headers from your Business Dashboard -> "
            "API Credentials page.\n\n"
            "All responses share one envelope: `{\"data\": ..., \"meta\": {...}}` on "
            "success, or `{\"error\": {\"code\", \"message\", \"retriable\"}}` on "
            "failure. See the Playground (/developers/playground.html) to try "
            "requests against your own account."
        ),
        routes=app.routes,
    )

    # Keep only operations actually tagged partner-api -- everything else
    # (dashboard/admin/billing/auth routes, which share the same FastAPI
    # app) is stripped out entirely, not just hidden in the UI.
    filtered_paths = {}
    for path, methods in full.get("paths", {}).items():
        kept = {method: op for method, op in methods.items()
                if PARTNER_TAG in op.get("tags", [])}
        if kept:
            filtered_paths[path] = kept
    full["paths"] = filtered_paths

    full["tags"] = [{"name": PARTNER_TAG,
                     "description": "Business farm CRUD + indices API"}]
    full.setdefault("components", {})["securitySchemes"] = {
        "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
        "ApiSecretAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Secret"},
    }
    full["security"] = [{"ApiKeyAuth": [], "ApiSecretAuth": []}]
    return full

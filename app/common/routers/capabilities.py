"""Stable capability and contract discovery endpoint."""

from fastapi import APIRouter

from app.common.config import settings


router = APIRouter(
    prefix="/api/common",
    tags=["Capabilities"],
)


@router.get("/capabilities", summary="Get supported API capabilities")
def capabilities() -> dict[str, object]:
    """Return feature support, independently from runtime health."""
    return {
        "api_version": "1.1",
        "service_version": settings.VERSION,
        "capabilities": {
            "upload": True,
            "gallery": True,
            "vision": True,
            "astro_records": True,
            "astro_changes": True,
            "geocoding": True,
            "places": True,
            "weather": True,
            "plate_solve": True,
        },
        "supported_services": ["MemoryKeeper", "AstroJournal"],
        "upload_contract": {
            "supports_service_name": True,
            "supports_upload_source": True,
            "upload_sources": ["mobile"],
            "supports_client_file_id": True,
            "supports_client_content_sha256": True,
            "supports_mobile_sha256_preflight": True,
            "mobile_sha256_preflight_endpoint": "/api/common/upload/preflight",
            "mobile_duplicate_scope": "MemoryKeeper",
            "upload_failure_codes": [
                "DUPLICATE_CONTENT",
                "CONTENT_SHA256_MISMATCH",
            ],
        },
    }

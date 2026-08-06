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
            "astro_records": False,
            "astro_changes": False,
            "plate_solve": False,
        },
        "supported_services": ["MemoryKeeper", "AstroJournal"],
        "upload_contract": {
            "supports_service_name": True,
            "supports_client_file_id": True,
            "supports_client_content_sha256": True,
        },
    }

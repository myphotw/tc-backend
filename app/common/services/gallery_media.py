from __future__ import annotations


def build_gallery_media_url(
    file_id: str,
    kind: str,
    storage_path: str | None,
) -> str | None:
    """Build the canonical Common Gallery media URL when an asset exists."""
    if not storage_path:
        return None
    return f"/api/common/gallery/{file_id}/{kind}"

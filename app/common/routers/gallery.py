"""Gallery Query API Router."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.common.schemas.gallery import (
    GalleryDetailResponse,
    GalleryListResponse,
    GallerySearchResponse,
    MapMarkerListResponse,
    StatisticsResponse,
    TimelineResponse,
)
from app.common.services.gallery_service import GalleryService, MediaKind

router = APIRouter(
    prefix="/api/common/gallery",
    tags=["Gallery"],
)

_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "Content-Disposition": "inline",
}


@router.get(
    "",
    response_model=GalleryListResponse,
    summary="Gallery list",
    description="사진 목록을 페이징/정렬하여 조회한다. 기본 정렬은 capture_datetime DESC.",
)
def list_gallery(
    page: int = Query(1, ge=1, description="페이지 번호 (1부터)"),
    page_size: int = Query(20, ge=1, le=200, description="페이지 크기"),
    sort: str = Query("capture_datetime_desc", description="정렬 키"),
    service_name: str = Query("MemoryKeeper", description="MemoryKeeper / AstroJournal"),
    db: Session = Depends(get_db),
) -> GalleryListResponse:
    """사진 목록을 조회한다."""
    return GalleryService(db).list_gallery(
        page=page,
        page_size=page_size,
        sort=sort,
        service_name=service_name,
    )


@router.get(
    "/search",
    response_model=GallerySearchResponse,
    summary="Gallery search",
    description="year/country/city/camera/tag/favorite/keyword 등 복수 조건 검색.",
)
def search_gallery(
    year: int | None = Query(None),
    country: str | None = Query(None),
    city: str | None = Query(None),
    camera: str | None = Query(None),
    tag: str | None = Query(None),
    favorite: bool | None = Query(None),
    service_name: str = Query("MemoryKeeper"),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    keyword: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort: str = Query("capture_datetime_desc"),
    db: Session = Depends(get_db),
) -> GallerySearchResponse:
    """복수 조건으로 사진을 검색한다."""
    return GalleryService(db).search(
        year=year,
        country=country,
        city=city,
        camera=camera,
        tag=tag,
        favorite=favorite,
        service_name=service_name,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword,
        page=page,
        page_size=page_size,
        sort=sort,
    )


@router.get(
    "/map",
    response_model=MapMarkerListResponse,
    summary="Gallery map markers",
    description="GPS가 있는 사진만 Marker로 반환한다.",
)
def map_gallery(
    service_name: str = Query("MemoryKeeper"),
    year: int | None = Query(None),
    db: Session = Depends(get_db),
) -> MapMarkerListResponse:
    """지도용 GPS Marker 목록을 조회한다."""
    return GalleryService(db).map_markers(
        service_name=service_name,
        year=year,
    )


@router.get(
    "/timeline",
    response_model=TimelineResponse,
    summary="Gallery timeline",
    description="촬영 연도별 사진 수를 반환한다.",
)
def timeline_gallery(
    service_name: str = Query("MemoryKeeper"),
    db: Session = Depends(get_db),
) -> TimelineResponse:
    """년도별 사진 그룹을 조회한다."""
    return GalleryService(db).timeline(service_name=service_name)


@router.get(
    "/statistics",
    response_model=StatisticsResponse,
    summary="Gallery statistics",
    description="전체/GPS/AI Tag/Camera/Country/Year/Service 통계를 반환한다.",
)
def statistics_gallery(
    service_name: str = Query("MemoryKeeper"),
    db: Session = Depends(get_db),
) -> StatisticsResponse:
    """Gallery 통계를 조회한다."""
    return GalleryService(db).statistics(service_name=service_name)


@router.get(
    "/{file_id}/thumbnail",
    summary="Gallery thumbnail",
    description="thumbnail 이미지 바이너리를 반환한다.",
    responses={404: {"description": "File or media not found"}},
)
def gallery_thumbnail(
    file_id: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    """thumbnail 이미지를 반환한다."""
    return _media_response(db, file_id=file_id, kind="thumbnail")


@router.get(
    "/{file_id}/preview",
    summary="Gallery preview",
    description="preview 이미지 바이너리를 반환한다.",
    responses={404: {"description": "File or media not found"}},
)
def gallery_preview(
    file_id: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    """preview 이미지를 반환한다."""
    return _media_response(db, file_id=file_id, kind="preview")


@router.get(
    "/{file_id}/original",
    summary="Gallery original",
    description="original 이미지 바이너리를 반환한다.",
    responses={404: {"description": "File or media not found"}},
)
def gallery_original(
    file_id: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    """original 이미지를 반환한다."""
    return _media_response(db, file_id=file_id, kind="original")


@router.get(
    "/{file_id}",
    response_model=GalleryDetailResponse,
    summary="Gallery detail",
    description="Metadata, AI/USER Tag, Storage Path, History 개수를 포함한 상세 조회.",
    responses={404: {"description": "File not found"}},
)
def gallery_detail(
    file_id: str,
    service_name: str | None = Query(None),
    db: Session = Depends(get_db),
) -> GalleryDetailResponse:
    """사진 상세를 조회한다."""
    return GalleryService(db).get_detail(file_id, service_name=service_name)


def _media_response(
    db: Session,
    *,
    file_id: str,
    kind: MediaKind,
) -> FileResponse:
    path, media_type = GalleryService(db).get_media(file_id=file_id, kind=kind)
    return FileResponse(
        path=path,
        media_type=media_type,
        headers=_CACHE_HEADERS,
    )

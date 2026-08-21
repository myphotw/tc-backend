from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.schemas.place import (
    FilePlaceResponse,
    FilePlaceUpdate,
    PlaceCreate,
    PlaceListResponse,
    PlaceMatchRequest,
    PlaceMatchResponse,
    PlaceResponse,
    PlaceUpdate,
    RadiusImpactRequest,
    RadiusImpactResponse,
    ReclassifyRequest,
    ReclassifyResponse,
)
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService


router = APIRouter(prefix="/api/memorykeeper", tags=["MemoryKeeper Places"])


@router.get("/places", response_model=PlaceListResponse)
def list_places(
    active: bool | None = Query(None),
    favorite: bool | None = Query(None),
    query: str | None = Query(None, max_length=200),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> PlaceListResponse:
    return MemoryKeeperPlaceService(db).list(active=active, favorite=favorite, query=query, limit=limit, offset=offset)


@router.post("/places", response_model=PlaceResponse, status_code=status.HTTP_201_CREATED)
def create_place(payload: PlaceCreate, db: Session = Depends(get_db)) -> PlaceResponse:
    return MemoryKeeperPlaceService(db).create(payload)


@router.post("/places/match", response_model=PlaceMatchResponse)
def match_place(payload: PlaceMatchRequest, db: Session = Depends(get_db)) -> PlaceMatchResponse:
    return MemoryKeeperPlaceService(db).match(payload)


@router.post("/places/radius-impact", response_model=RadiusImpactResponse)
def radius_impact(payload: RadiusImpactRequest, db: Session = Depends(get_db)) -> RadiusImpactResponse:
    return MemoryKeeperPlaceService(db).radius_impact(payload)


@router.get("/places/{place_id}", response_model=PlaceResponse)
def get_place(place_id: str, db: Session = Depends(get_db)) -> PlaceResponse:
    return MemoryKeeperPlaceService(db).get(place_id)


@router.patch("/places/{place_id}", response_model=PlaceResponse)
def update_place(place_id: str, payload: PlaceUpdate, db: Session = Depends(get_db)) -> PlaceResponse:
    return MemoryKeeperPlaceService(db).update(place_id, payload)


@router.delete("/places/{place_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_place(place_id: str, db: Session = Depends(get_db)) -> Response:
    MemoryKeeperPlaceService(db).delete(place_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/places/{place_id}/reclassify", response_model=ReclassifyResponse)
def reclassify_place(
    place_id: str,
    payload: ReclassifyRequest,
    db: Session = Depends(get_db),
) -> ReclassifyResponse:
    return MemoryKeeperPlaceService(db).reclassify(place_id, reassign_from_other_places=payload.reassign_from_other_places)


@router.patch("/files/{file_id}/place", response_model=FilePlaceResponse)
def assign_file_place(
    file_id: str,
    payload: FilePlaceUpdate,
    db: Session = Depends(get_db),
) -> FilePlaceResponse:
    return MemoryKeeperPlaceService(db).assign_file(
        public_file_id=file_id,
        place_id=str(payload.memorykeeper_place_id) if payload.memorykeeper_place_id else None,
        expected_revision=payload.expected_revision,
    )

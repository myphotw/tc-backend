from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.astrojournal.schemas.observation_record import (
    ObservationRecordCreate,
    ObservationRecordResponse,
    ObservationRecordUpdate,
)
from app.astrojournal.services.observation_record_service import ObservationRecordService
from app.common.database import get_db

router = APIRouter(prefix="/api/astro/records", tags=["AstroJournal Observation Records"])


@router.post("", response_model=ObservationRecordResponse, status_code=status.HTTP_201_CREATED)
def create_record(
    payload: ObservationRecordCreate,
    db: Session = Depends(get_db),
) -> ObservationRecordResponse:
    return ObservationRecordService(db).create(payload)


@router.get("", response_model=list[ObservationRecordResponse])
def list_records(
    catalog_object_id: str | None = Query(None),
    favorite: bool | None = Query(None),
    representative: bool | None = Query(None),
    db: Session = Depends(get_db),
) -> list[ObservationRecordResponse]:
    return ObservationRecordService(db).list(
        catalog_object_id=catalog_object_id,
        favorite=favorite,
        representative=representative,
    )


@router.get("/{record_id}", response_model=ObservationRecordResponse)
def get_record(record_id: str, db: Session = Depends(get_db)) -> ObservationRecordResponse:
    return ObservationRecordService(db).get(record_id)


@router.patch("/{record_id}", response_model=ObservationRecordResponse)
def update_record(
    record_id: str,
    payload: ObservationRecordUpdate,
    db: Session = Depends(get_db),
) -> ObservationRecordResponse:
    return ObservationRecordService(db).update(record_id, payload)


@router.delete("/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_record(record_id: str, db: Session = Depends(get_db)) -> Response:
    ObservationRecordService(db).soft_delete(record_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

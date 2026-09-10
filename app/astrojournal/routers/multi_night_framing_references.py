from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.astrojournal.schemas.multi_night_framing_reference import (
    MultiNightFramingReferenceConflictResponse,
    MultiNightFramingReferenceCreate,
    MultiNightFramingReferenceDeleteResponse,
    MultiNightFramingReferenceResponse,
    MultiNightFramingReferenceUpdate,
)
from app.astrojournal.services.multi_night_framing_reference_service import (
    MultiNightFramingReferenceService,
)
from app.common.database import get_db


router = APIRouter(
    prefix="/api/astro/multi-night-framing-references",
    tags=["AstroJournal Multi-Night Framing"],
)


@router.get("", response_model=list[MultiNightFramingReferenceResponse])
def list_multi_night_framing_references(
    catalog_object_id: str | None = Query(None, max_length=255),
    equipment_id: UUID | None = Query(None),
    db: Session = Depends(get_db),
) -> list[MultiNightFramingReferenceResponse]:
    return MultiNightFramingReferenceService(db).list(
        catalog_object_id=catalog_object_id,
        equipment_id=equipment_id,
    )


@router.post(
    "",
    response_model=MultiNightFramingReferenceResponse,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": MultiNightFramingReferenceConflictResponse}},
)
def create_multi_night_framing_reference(
    payload: MultiNightFramingReferenceCreate,
    db: Session = Depends(get_db),
) -> MultiNightFramingReferenceResponse:
    return MultiNightFramingReferenceService(db).create(payload)


@router.get("/{reference_id}", response_model=MultiNightFramingReferenceResponse)
def get_multi_night_framing_reference(
    reference_id: str,
    db: Session = Depends(get_db),
) -> MultiNightFramingReferenceResponse:
    return MultiNightFramingReferenceService(db).get(reference_id)


@router.patch(
    "/{reference_id}",
    response_model=MultiNightFramingReferenceResponse,
    responses={409: {"model": MultiNightFramingReferenceConflictResponse}},
)
def update_multi_night_framing_reference(
    reference_id: str,
    payload: MultiNightFramingReferenceUpdate,
    db: Session = Depends(get_db),
) -> MultiNightFramingReferenceResponse:
    return MultiNightFramingReferenceService(db).update(reference_id, payload)


@router.delete(
    "/{reference_id}",
    response_model=MultiNightFramingReferenceDeleteResponse,
    responses={409: {"model": MultiNightFramingReferenceConflictResponse}},
)
def delete_multi_night_framing_reference(
    reference_id: str,
    expected_revision: int = Query(..., ge=1),
    db: Session = Depends(get_db),
) -> MultiNightFramingReferenceDeleteResponse:
    return MultiNightFramingReferenceService(db).soft_delete(
        reference_id,
        expected_revision=expected_revision,
    )

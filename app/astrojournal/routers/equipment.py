from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.astrojournal.schemas.equipment import (
    EquipmentConflictResponse,
    EquipmentCreate,
    EquipmentDeleteResponse,
    EquipmentResponse,
    EquipmentUpdate,
)
from app.astrojournal.services.equipment_service import EquipmentService
from app.common.database import get_db


router = APIRouter(
    prefix="/api/astro/equipment",
    tags=["AstroJournal Equipment"],
)


@router.get("", response_model=list[EquipmentResponse])
def list_equipment(
    db: Session = Depends(get_db),
) -> list[EquipmentResponse]:
    return EquipmentService(db).list()


@router.post(
    "",
    response_model=EquipmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_equipment(
    payload: EquipmentCreate,
    db: Session = Depends(get_db),
) -> EquipmentResponse:
    return EquipmentService(db).create(payload)


@router.get("/{equipment_id}", response_model=EquipmentResponse)
def get_equipment(
    equipment_id: str,
    db: Session = Depends(get_db),
) -> EquipmentResponse:
    return EquipmentService(db).get(equipment_id)


@router.patch(
    "/{equipment_id}",
    response_model=EquipmentResponse,
    responses={409: {"model": EquipmentConflictResponse}},
)
def update_equipment(
    equipment_id: str,
    payload: EquipmentUpdate,
    db: Session = Depends(get_db),
) -> EquipmentResponse:
    return EquipmentService(db).update(equipment_id, payload)


@router.delete(
    "/{equipment_id}",
    response_model=EquipmentDeleteResponse,
    responses={409: {"model": EquipmentConflictResponse}},
)
def delete_equipment(
    equipment_id: str,
    expected_revision: int = Query(..., ge=1),
    db: Session = Depends(get_db),
) -> EquipmentDeleteResponse:
    return EquipmentService(db).soft_delete(
        equipment_id,
        expected_revision=expected_revision,
    )

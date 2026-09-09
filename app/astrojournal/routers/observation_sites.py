from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.astrojournal.schemas.observation_site import (
    ObservationSiteConflictResponse,
    ObservationSiteCreate,
    ObservationSiteDeleteResponse,
    ObservationSiteResponse,
    ObservationSiteUpdate,
)
from app.astrojournal.services.observation_site_service import (
    ObservationSiteService,
)
from app.common.database import get_db


router = APIRouter(
    prefix="/api/astro/observation-sites",
    tags=["AstroJournal Observation Sites"],
)


@router.get("", response_model=list[ObservationSiteResponse])
def list_observation_sites(
    db: Session = Depends(get_db),
) -> list[ObservationSiteResponse]:
    return ObservationSiteService(db).list()


@router.post(
    "",
    response_model=ObservationSiteResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_observation_site(
    payload: ObservationSiteCreate,
    db: Session = Depends(get_db),
) -> ObservationSiteResponse:
    return ObservationSiteService(db).create(payload)


@router.get("/{site_id}", response_model=ObservationSiteResponse)
def get_observation_site(
    site_id: str,
    db: Session = Depends(get_db),
) -> ObservationSiteResponse:
    return ObservationSiteService(db).get(site_id)


@router.patch(
    "/{site_id}",
    response_model=ObservationSiteResponse,
    responses={409: {"model": ObservationSiteConflictResponse}},
)
def update_observation_site(
    site_id: str,
    payload: ObservationSiteUpdate,
    db: Session = Depends(get_db),
) -> ObservationSiteResponse:
    return ObservationSiteService(db).update(site_id, payload)


@router.delete(
    "/{site_id}",
    response_model=ObservationSiteDeleteResponse,
    responses={409: {"model": ObservationSiteConflictResponse}},
)
def delete_observation_site(
    site_id: str,
    expected_revision: int = Query(..., ge=1),
    db: Session = Depends(get_db),
) -> ObservationSiteDeleteResponse:
    return ObservationSiteService(db).soft_delete(
        site_id,
        expected_revision=expected_revision,
    )

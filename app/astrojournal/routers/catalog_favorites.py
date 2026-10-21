from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.astrojournal.schemas.catalog_favorite import (
    CatalogFavoriteCreate,
    CatalogFavoriteDeleteResponse,
    CatalogFavoriteResponse,
)
from app.astrojournal.services.catalog_favorite_service import CatalogFavoriteService
from app.common.database import get_db


router = APIRouter(
    prefix="/api/astro/catalog-favorites",
    tags=["AstroJournal Catalog Favorites"],
)


@router.get("", response_model=list[CatalogFavoriteResponse])
def list_catalog_favorites(
    db: Session = Depends(get_db),
) -> list[CatalogFavoriteResponse]:
    return CatalogFavoriteService(db).list()


@router.post("", response_model=CatalogFavoriteResponse)
def add_catalog_favorite(
    payload: CatalogFavoriteCreate,
    db: Session = Depends(get_db),
) -> CatalogFavoriteResponse:
    return CatalogFavoriteService(db).add(payload)


@router.delete("/{catalog_object_id}", response_model=CatalogFavoriteDeleteResponse)
def remove_catalog_favorite(
    catalog_object_id: str,
    db: Session = Depends(get_db),
) -> CatalogFavoriteDeleteResponse:
    return CatalogFavoriteService(db).remove(catalog_object_id)

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.astrojournal.repositories.catalog_favorite_repository import (
    CatalogFavoriteRepository,
)
from app.astrojournal.schemas.catalog_favorite import (
    CatalogFavoriteCreate,
    CatalogFavoriteDeleteResponse,
    CatalogFavoriteResponse,
)


class CatalogFavoriteService:
    """Manage the one shared AstroJournal favorite set."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = CatalogFavoriteRepository(db)

    def list(self) -> list[CatalogFavoriteResponse]:
        return [
            CatalogFavoriteResponse.model_validate(item)
            for item in self.repository.list()
        ]

    def add(self, payload: CatalogFavoriteCreate) -> CatalogFavoriteResponse:
        object_id = payload.catalog_object_id
        existing = self.repository.get(object_id)
        if existing is not None:
            return CatalogFavoriteResponse.model_validate(existing)

        try:
            favorite = self.repository.create(object_id)
            self.db.commit()
            self.db.refresh(favorite)
            return CatalogFavoriteResponse.model_validate(favorite)
        except IntegrityError:
            # Concurrent duplicate requests are idempotent.
            self.db.rollback()
            existing = self.repository.get(object_id)
            if existing is None:
                raise
            return CatalogFavoriteResponse.model_validate(existing)
        except Exception:
            self.db.rollback()
            raise

    def remove(self, catalog_object_id: str) -> CatalogFavoriteDeleteResponse:
        object_id = CatalogFavoriteCreate(
            catalog_object_id=catalog_object_id,
        ).catalog_object_id
        favorite = self.repository.get(object_id)
        if favorite is None:
            return CatalogFavoriteDeleteResponse(
                catalog_object_id=object_id,
                deleted=False,
            )

        try:
            self.repository.delete(favorite)
            self.db.commit()
            return CatalogFavoriteDeleteResponse(
                catalog_object_id=object_id,
                deleted=True,
            )
        except Exception:
            self.db.rollback()
            raise

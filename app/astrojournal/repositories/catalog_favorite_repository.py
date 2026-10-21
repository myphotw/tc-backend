from __future__ import annotations

from sqlalchemy.orm import Session

from app.astrojournal.models.catalog_favorite import AstroCatalogFavorite


class CatalogFavoriteRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, catalog_object_id: str) -> AstroCatalogFavorite | None:
        return self.db.get(AstroCatalogFavorite, catalog_object_id)

    def list(self) -> list[AstroCatalogFavorite]:
        return (
            self.db.query(AstroCatalogFavorite)
            .order_by(
                AstroCatalogFavorite.created_at.asc(),
                AstroCatalogFavorite.catalog_object_id.asc(),
            )
            .all()
        )

    def create(self, catalog_object_id: str) -> AstroCatalogFavorite:
        favorite = AstroCatalogFavorite(catalog_object_id=catalog_object_id)
        self.db.add(favorite)
        self.db.flush()
        return favorite

    def delete(self, favorite: AstroCatalogFavorite) -> None:
        self.db.delete(favorite)
        self.db.flush()

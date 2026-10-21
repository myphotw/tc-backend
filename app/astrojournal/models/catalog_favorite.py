from __future__ import annotations

from sqlalchemy import Column, DateTime, String
from sqlalchemy.sql import func

from app.common.model_base import Base
from app.common.schema_sync import migration_managed_schema_info


class AstroCatalogFavorite(Base):
    """One shared AstroJournal catalog favorite.

    Favorites are deliberately global to the AstroJournal service.  They are
    not partitioned by user, device, or observation site.
    """

    __tablename__ = "astro_catalog_favorites"
    __table_args__ = ({"info": migration_managed_schema_info()},)

    catalog_object_id = Column(String(255), primary_key=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

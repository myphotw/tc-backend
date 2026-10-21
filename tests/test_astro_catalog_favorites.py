from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.catalog_favorite import AstroCatalogFavorite
from app.astrojournal.schemas.catalog_favorite import CatalogFavoriteCreate
from app.astrojournal.services.catalog_favorite_service import CatalogFavoriteService
from app.common.model_registry import Base


class AstroCatalogFavoriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.service = CatalogFavoriteService(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_add_list_and_duplicate_add_are_idempotent(self) -> None:
        first = self.service.add(CatalogFavoriteCreate(catalog_object_id="solar_6"))
        replay = self.service.add(CatalogFavoriteCreate(catalog_object_id="solar_6"))

        self.assertEqual(first, replay)
        self.assertEqual(
            [item.catalog_object_id for item in self.service.list()],
            ["solar_6"],
        )
        self.assertEqual(self.db.query(AstroCatalogFavorite).count(), 1)

    def test_remove_and_repeated_remove_are_idempotent(self) -> None:
        self.service.add(CatalogFavoriteCreate(catalog_object_id="NGC2237"))

        removed = self.service.remove("NGC2237")
        repeated = self.service.remove("NGC2237")

        self.assertTrue(removed.deleted)
        self.assertFalse(repeated.deleted)
        self.assertEqual(self.service.list(), [])

    def test_id_is_trimmed_and_invalid_ids_fail_closed(self) -> None:
        favorite = self.service.add(
            CatalogFavoriteCreate(catalog_object_id="  solar_2  ")
        )
        self.assertEqual(favorite.catalog_object_id, "solar_2")

        for value in ("   ", "x" * 256):
            with self.subTest(value_length=len(value)):
                with self.assertRaises(ValidationError):
                    CatalogFavoriteCreate(catalog_object_id=value)

    def test_database_backed_list_survives_new_session(self) -> None:
        self.db.close()
        self.engine.dispose()
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "favorites.sqlite"
            engine = create_engine(f"sqlite:///{database_path}")
            Base.metadata.create_all(engine)
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            first_session = factory()
            CatalogFavoriteService(first_session).add(
                CatalogFavoriteCreate(catalog_object_id="solar_7")
            )
            first_session.close()

            second_session = factory()
            items = CatalogFavoriteService(second_session).list()
            second_session.close()
            engine.dispose()

        self.assertEqual([item.catalog_object_id for item in items], ["solar_7"])

    def test_schema_is_global_without_user_or_site_partition(self) -> None:
        columns = set(AstroCatalogFavorite.__table__.columns.keys())
        self.assertEqual(
            columns,
            {"catalog_object_id", "created_at", "updated_at"},
        )

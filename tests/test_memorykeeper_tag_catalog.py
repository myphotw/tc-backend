from __future__ import annotations

import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.repositories.gallery_repository import GalleryRepository
from app.common.repositories.tag_repository import TagSource, TagType
from app.common.services.gallery_service import GalleryService
from app.common.schema_sync import initialize_database
from app.main import app
from app.memorykeeper.models.tag import Tag
from app.memorykeeper.models.tag_canonical_override import (
    MemoryKeeperTagCanonicalOverride,
)
from app.memorykeeper.schemas.tag import TagCreate, UnifiedTagRenameRequest
from app.memorykeeper.services.tag_catalog_service import (
    MemoryKeeperTagCatalogService,
)
from app.memorykeeper.services.tag_service import MemoryKeeperTagService


class MemoryKeeperUnifiedTagCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.catalog = MemoryKeeperTagCatalogService(self.db)
        self.tags = MemoryKeeperTagService(self.db)
        self.counter = 0

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def file(self, service_name: str = "MemoryKeeper") -> CommonFile:
        self.counter += 1
        digest = f"{self.counter:064x}"
        item = CommonFile(
            file_id=digest,
            original_name=f"{digest}.jpg",
            service_name=service_name,
            deleted=False,
        )
        self.db.add(item)
        self.db.flush()
        self.db.add(CommonFileService(file_id=item.id, service_name=service_name))
        self.db.commit()
        return item

    def raw(self, file: CommonFile, name: str, confidence: float = 95) -> CommonFileTag:
        relation = CommonFileTag(
            file_id=file.id,
            tag=name,
            tag_type=TagType.AI,
            source=TagSource.AI,
            confidence=confidence,
            deleted=False,
        )
        self.db.add(relation)
        self.db.commit()
        return relation

    def master(self, name: str) -> Tag:
        response = self.tags.create(TagCreate(name=name))
        return self.db.get(Tag, response.id)

    def user(self, file: CommonFile, tag: Tag) -> CommonFileTag:
        relation = CommonFileTag(
            file_id=file.id,
            memorykeeper_tag_id=tag.id,
            tag=tag.tag_name,
            tag_type=TagType.USER,
            source=TagSource.USER,
            confidence=None,
            deleted=False,
        )
        self.db.add(relation)
        self.db.commit()
        return relation

    def item(self, display_name: str):
        response = self.catalog.list(query=None, limit=200, offset=0)
        return next(item for item in response.items if item.display_name == display_name)

    def test_user_and_canonical_share_one_identity_and_usage_is_distinct(self) -> None:
        first = self.file()
        second = self.file()
        third = self.file()
        self.raw(first, "Dog")
        self.raw(second, "Puppy")
        self.raw(third, "Canidae")
        dog = self.master("강아지")
        self.user(first, dog)

        response = self.catalog.list(query=None, limit=200, offset=0)
        dog_items = [item for item in response.items if item.display_name == "강아지"]

        self.assertEqual(len(dog_items), 1)
        self.assertEqual(dog_items[0].identity, f"tag:{dog.id}")
        self.assertEqual(dog_items[0].usage_count, 3)
        self.assertEqual(dog_items[0].canonical_references, ["dog"])
        self.assertNotIn("source", dog_items[0].model_dump())

    def test_ai_rename_promotes_global_override_and_preserves_raw(self) -> None:
        first = self.file()
        second = self.file()
        self.raw(first, "Dog")
        self.raw(second, "Puppy")

        renamed = self.catalog.rename_or_merge(
            "ai:dog",
            UnifiedTagRenameRequest(name="반려동물", revision=1),
        )

        self.assertTrue(renamed.identity.startswith("tag:"))
        self.assertEqual(renamed.display_name, "반려동물")
        self.assertEqual(renamed.usage_count, 2)
        self.assertEqual(renamed.canonical_references, ["dog"])
        self.assertEqual(
            {row.tag for row in self.db.query(CommonFileTag).filter_by(source="AI")},
            {"Dog", "Puppy"},
        )

        future = self.file()
        self.raw(future, "Canidae")
        detail = GalleryService(self.db).get_detail(
            future.file_id,
            service_name="MemoryKeeper",
        )
        self.assertEqual([tag.tag for tag in detail.tags], ["반려동물"])
        self.assertEqual(detail.tags[0].identity, renamed.identity)

        rows, total = GalleryRepository(self.db).search(
            tag="반려동물",
            service_name="MemoryKeeper",
        )
        self.assertEqual(total, 3)
        self.assertEqual({row[0].id for row in rows}, {first.id, second.id, future.id})

    def test_ai_rename_to_existing_name_uses_existing_master(self) -> None:
        first = self.file()
        second = self.file()
        existing_file = self.file()
        self.raw(first, "Dog")
        self.raw(second, "Puppy")
        existing = self.master("반려동물")
        self.user(existing_file, existing)

        result = self.catalog.rename_or_merge(
            "ai:dog",
            UnifiedTagRenameRequest(name="반려동물", revision=1),
        )

        self.assertEqual(result.identity, f"tag:{existing.id}")
        self.assertEqual(result.usage_count, 3)
        self.assertEqual(
            self.db.query(Tag).filter(Tag.deleted.is_(False)).count(),
            1,
        )

    def test_user_rename_to_new_name_keeps_relations_and_canonical_override(self) -> None:
        relation_file = self.file()
        raw_file = self.file()
        self.raw(raw_file, "Dog")
        source = self.master("강아지")
        relation = self.user(relation_file, source)
        original_relation_id = relation.id

        result = self.catalog.rename_or_merge(
            f"tag:{source.id}",
            UnifiedTagRenameRequest(name="반려동물", revision=1),
        )

        self.assertEqual(result.identity, f"tag:{source.id}")
        self.assertEqual(result.display_name, "반려동물")
        self.assertEqual(result.usage_count, 2)
        self.db.refresh(relation)
        self.assertEqual(relation.id, original_relation_id)
        self.assertEqual(relation.tag, "반려동물")
        override = self.db.query(MemoryKeeperTagCanonicalOverride).one()
        self.assertEqual(override.canonical_key, "dog")
        self.assertEqual(override.memorykeeper_tag_id, source.id)
        self.assertFalse(override.suppressed)

    def test_rename_to_existing_name_merges_without_duplicate_relations(self) -> None:
        first = self.file()
        second = self.file()
        third = self.file()
        source = self.master("강아지")
        target = self.master("반려동물")
        self.user(first, source)
        self.user(second, target)
        self.raw(third, "Dog")

        result = self.catalog.rename_or_merge(
            f"tag:{source.id}",
            UnifiedTagRenameRequest(name="반려동물", revision=1),
        )

        self.assertEqual(result.identity, f"tag:{target.id}")
        self.assertEqual(result.usage_count, 3)
        self.assertTrue(self.db.get(Tag, source.id).deleted)
        active_relations = (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.memorykeeper_tag_id == target.id)
            .filter(CommonFileTag.deleted.is_(False))
            .all()
        )
        self.assertEqual({relation.file_id for relation in active_relations}, {first.id, second.id})
        self.assertEqual(len(active_relations), 2)
        override = self.db.query(MemoryKeeperTagCanonicalOverride).one()
        self.assertEqual(override.memorykeeper_tag_id, target.id)

    def test_delete_ai_suppresses_projection_search_and_keeps_raw(self) -> None:
        item = self.file()
        raw = self.raw(item, "Dog")

        self.catalog.delete("ai:dog", expected_revision=1)

        self.assertEqual(self.catalog.list(query=None, limit=200, offset=0).items, [])
        detail = GalleryService(self.db).get_detail(
            item.file_id,
            service_name="MemoryKeeper",
        )
        self.assertEqual(detail.tags, [])
        _rows, total = GalleryRepository(self.db).search(
            tag="강아지",
            service_name="MemoryKeeper",
        )
        self.assertEqual(total, 0)
        self.db.refresh(raw)
        self.assertFalse(raw.deleted)
        self.assertTrue(self.db.query(MemoryKeeperTagCanonicalOverride).one().suppressed)

    def test_delete_user_identity_suppresses_linked_canonical(self) -> None:
        assigned_file = self.file()
        raw_file = self.file()
        dog = self.master("강아지")
        relation = self.user(assigned_file, dog)
        raw = self.raw(raw_file, "Dog")

        self.catalog.delete(f"tag:{dog.id}", expected_revision=1)

        self.assertTrue(self.db.get(Tag, dog.id).deleted)
        self.db.refresh(relation)
        self.db.refresh(raw)
        self.assertTrue(relation.deleted)
        self.assertFalse(raw.deleted)
        self.assertTrue(self.db.query(MemoryKeeperTagCanonicalOverride).one().suppressed)
        self.assertEqual(self.catalog.list(query=None, limit=200, offset=0).total, 0)

    def test_revision_conflict_and_astrojournal_raw_regression(self) -> None:
        memory_file = self.file()
        self.raw(memory_file, "Dog")
        with self.assertRaises(HTTPException) as raised:
            self.catalog.rename_or_merge(
                "ai:dog",
                UnifiedTagRenameRequest(name="반려동물", revision=2),
            )
        self.assertEqual(raised.exception.status_code, 409)

        astro_file = self.file("AstroJournal")
        self.raw(astro_file, "Dog")
        detail = GalleryService(self.db).get_detail(
            astro_file.file_id,
            service_name="AstroJournal",
        )
        self.assertEqual([tag.tag for tag in detail.ai_tags], ["Dog"])

    def test_schema_and_openapi_are_additive(self) -> None:
        initialize_database(self.engine)
        initialize_database(self.engine)
        inspector = inspect(self.engine)
        self.assertIn("mk_tag_canonical_overrides", inspector.get_table_names())
        indexes = {
            item["name"]
            for item in inspector.get_indexes("mk_tag_canonical_overrides")
        }
        self.assertIn("uq_mk_tag_canonical_overrides_key", indexes)
        foreign_keys = inspector.get_foreign_keys("mk_tag_canonical_overrides")
        self.assertTrue(
            any(item["referred_table"] == "mk_tags" for item in foreign_keys)
        )

        schema = app.openapi()
        self.assertIn("/api/memorykeeper/tags/catalog", schema["paths"])
        self.assertIn(
            "/api/memorykeeper/tags/catalog/{identity}",
            schema["paths"],
        )
        properties = schema["components"]["schemas"]["UnifiedTagCatalogItem"][
            "properties"
        ]
        self.assertNotIn("source", properties)
        self.assertIn("canonical_references", properties)


if __name__ == "__main__":
    unittest.main()

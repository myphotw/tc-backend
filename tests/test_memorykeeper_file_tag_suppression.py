from __future__ import annotations

import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from app.astrojournal.services.file_cleanup_service import AstroJournalFileCleanupService
from app.common.database import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.repositories.gallery_repository import GalleryRepository
from app.common.repositories.tag_repository import TagRepository, TagSource, TagType
from app.common.schema_sync import initialize_database
from app.common.services.gallery_service import GalleryService
from app.main import app
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.file_tag_suppression import (
    MemoryKeeperFileTagSuppression,
)
from app.memorykeeper.models.tag import Tag
from app.memorykeeper.models.tag_canonical_override import (
    MemoryKeeperTagCanonicalOverride,
)
from app.memorykeeper.schemas.tag import TagCreate, UnifiedTagRenameRequest
from app.memorykeeper.services.file_service import MemoryKeeperFileService
from app.memorykeeper.services.file_tag_visibility_service import (
    MemoryKeeperFileTagVisibilityService,
)
from app.memorykeeper.services.tag_catalog_service import (
    MemoryKeeperTagCatalogService,
)
from app.memorykeeper.services.tag_service import MemoryKeeperTagService


class MemoryKeeperFileTagSuppressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.visibility = MemoryKeeperFileTagVisibilityService(self.db)
        self.catalog = MemoryKeeperTagCatalogService(self.db)
        self.tags = MemoryKeeperTagService(self.db)
        self.counter = 0

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def file(self, services: tuple[str, ...] = ("MemoryKeeper",)) -> CommonFile:
        self.counter += 1
        digest = f"{self.counter:064x}"
        item = CommonFile(
            file_id=digest,
            original_name=f"{digest}.jpg",
            service_name=services[0],
            deleted=False,
        )
        self.db.add(item)
        self.db.flush()
        for service in services:
            self.db.add(CommonFileService(file_id=item.id, service_name=service))
        self.db.commit()
        return item

    def raw(self, file: CommonFile, name: str, confidence: float = 95) -> CommonFileTag:
        item = CommonFileTag(
            file_id=file.id,
            tag=name,
            tag_type=TagType.AI,
            source=TagSource.AI,
            confidence=confidence,
            deleted=False,
        )
        self.db.add(item)
        self.db.commit()
        return item

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

    def catalog_item(self, identity: str):
        return next(
            item
            for item in self.catalog.list(query=None, limit=200, offset=0).items
            if item.identity == identity
        )

    def projected_names(self, file: CommonFile, service: str = "MemoryKeeper") -> list[str]:
        detail = GalleryService(self.db).get_detail(file.file_id, service_name=service)
        return [tag.tag for tag in detail.tags]

    def test_ai_suppression_is_file_scoped_and_updates_every_read_projection(self) -> None:
        first = self.file()
        second = self.file()
        third = self.file()
        raw = self.raw(first, "Dog", 91)
        self.raw(second, "Puppy", 92)
        self.raw(third, "Canidae", 93)

        result = self.visibility.hide(first.file_id, "ai:dog", expected_revision=0)

        self.assertTrue(result.hidden)
        self.assertEqual(result.revision, 1)
        self.assertNotIn("강아지", self.projected_names(first))
        self.assertIn("강아지", self.projected_names(second))
        self.assertIn("강아지", self.projected_names(third))
        self.db.refresh(raw)
        self.assertFalse(raw.deleted)
        self.assertEqual(raw.confidence, 91)
        self.assertEqual(self.db.query(MemoryKeeperTagCanonicalOverride).count(), 0)
        self.assertEqual(self.catalog_item("ai:dog").usage_count, 2)
        repository = GalleryRepository(self.db)
        for value in ("강아지", "dog"):
            rows, total = repository.search(tag=value, service_name="MemoryKeeper")
            self.assertEqual(total, 2)
            self.assertEqual({row[0].id for row in rows}, {second.id, third.id})
        rows, total = repository.search(keyword="dog", service_name="MemoryKeeper")
        self.assertEqual(total, 2)
        self.assertEqual({row[0].id for row in rows}, {second.id, third.id})
        state = self.db.get(MemoryKeeperFileState, first.id)
        self.assertEqual(state.revision, 1)
        event = self.db.query(CommonChangeEvent).filter_by(
            resource_type="MemoryKeeperFileTag"
        ).one()
        self.assertEqual(event.operation, "DELETE")
        self.assertTrue(event.tombstone)
        self.assertEqual(event.revision, 1)

    def test_stale_revision_is_conflict_and_does_not_mutate(self) -> None:
        item = self.file()
        self.raw(item, "Dog")

        with self.assertRaises(HTTPException) as raised:
            self.visibility.hide(item.file_id, "ai:dog", expected_revision=1)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "REVISION_CONFLICT")
        self.assertEqual(self.db.query(MemoryKeeperFileTagSuppression).count(), 0)
        self.assertIn("강아지", self.projected_names(item))

    def test_restore_and_vision_reprocess_preserve_expected_semantics(self) -> None:
        item = self.file()
        raw = self.raw(item, "Dog", 90)
        self.visibility.hide(item.file_id, "ai:dog", expected_revision=0)

        TagRepository(self.db).save_ai_tag(file_id=item.id, tag="Dog", confidence=99)
        TagRepository(self.db).save_ai_tag(file_id=item.id, tag="Puppy", confidence=98)
        self.assertEqual(self.projected_names(item), [])
        self.db.refresh(raw)
        self.assertEqual(raw.confidence, 99)

        restored = self.visibility.restore(item.file_id, "ai:dog", expected_revision=1)

        self.assertFalse(restored.hidden)
        self.assertEqual(restored.revision, 2)
        self.assertIn("강아지", self.projected_names(item))
        self.assertEqual(self.catalog_item("ai:dog").usage_count, 1)
        suppression = self.db.query(MemoryKeeperFileTagSuppression).one()
        self.assertTrue(suppression.deleted)
        events = self.db.query(CommonChangeEvent).filter_by(
            resource_type="MemoryKeeperFileTag"
        ).order_by(CommonChangeEvent.id).all()
        self.assertEqual([event.operation for event in events], ["DELETE", "UPDATE"])
        self.assertFalse(events[-1].tombstone)

    def test_catalog_rename_does_not_resurface_hidden_semantic_identity(self) -> None:
        hidden = self.file()
        visible = self.file()
        self.raw(hidden, "Dog")
        self.raw(visible, "Puppy")
        self.visibility.hide(hidden.file_id, "ai:dog", expected_revision=0)

        renamed = self.catalog.rename_or_merge(
            "ai:dog",
            UnifiedTagRenameRequest(name="반려견", revision=1),
        )

        self.assertNotIn("반려견", self.projected_names(hidden))
        self.assertIn("반려견", self.projected_names(visible))
        self.assertEqual(self.catalog_item(renamed.identity).usage_count, 1)

    def test_user_and_ai_same_meaning_hide_and_restore_without_ai_fallback(self) -> None:
        item = self.file()
        raw = self.raw(item, "Dog")
        master = self.master("강아지")
        relation = self.user(item, master)

        hidden = self.visibility.hide(
            item.file_id,
            f"tag:{master.id}",
            expected_revision=0,
        )

        self.db.refresh(relation)
        self.db.refresh(raw)
        self.assertTrue(relation.deleted)
        self.assertFalse(raw.deleted)
        self.assertEqual(self.projected_names(item), [])
        self.assertEqual(self.catalog_item(f"tag:{master.id}").usage_count, 0)

        restored = self.visibility.restore(
            item.file_id,
            f"tag:{master.id}",
            expected_revision=hidden.revision,
        )
        self.db.refresh(relation)
        self.assertFalse(relation.deleted)
        self.assertFalse(restored.hidden)
        self.assertEqual(self.projected_names(item), ["강아지"])

    def test_existing_numeric_assign_also_restores_linked_canonical(self) -> None:
        item = self.file()
        self.raw(item, "Dog")
        master = self.master("강아지")
        self.user(item, master)
        hidden = self.visibility.hide(
            item.file_id,
            f"tag:{master.id}",
            expected_revision=0,
        )

        assigned = self.tags.assign(
            item.file_id,
            master.id,
            expected_revision=hidden.revision,
        )

        self.assertTrue(assigned.assigned)
        self.assertEqual(assigned.revision, 2)
        self.assertEqual(self.projected_names(item), ["강아지"])
        self.assertTrue(self.db.query(MemoryKeeperFileTagSuppression).one().deleted)

    def test_merge_propagates_suppression_across_canonical_cluster(self) -> None:
        item = self.file()
        dog = self.master("강아지")
        cat = self.master("고양이")
        self.user(item, dog)
        self.visibility.hide(item.file_id, f"tag:{dog.id}", expected_revision=0)

        merged = self.catalog.rename_or_merge(
            f"tag:{dog.id}",
            UnifiedTagRenameRequest(name="고양이", revision=dog.revision),
        )
        self.raw(item, "Cat")

        self.assertEqual(merged.identity, f"tag:{cat.id}")
        self.assertEqual(self.projected_names(item), [])
        active = {
            row.canonical_key
            for row in self.db.query(MemoryKeeperFileTagSuppression)
            .filter_by(file_id=item.id, deleted=False)
        }
        self.assertEqual(active, {"dog", "cat"})

    def test_service_isolation_and_shared_file_only_affect_memorykeeper(self) -> None:
        astro_only = self.file(("AstroJournal",))
        self.raw(astro_only, "Dog")
        with self.assertRaises(HTTPException) as raised:
            self.visibility.hide(astro_only.file_id, "ai:dog", expected_revision=0)
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.db.query(MemoryKeeperFileTagSuppression).count(), 0)

        shared = self.file(("MemoryKeeper", "AstroJournal"))
        self.raw(shared, "Dog")
        self.visibility.hide(shared.file_id, "ai:dog", expected_revision=0)
        self.assertEqual(self.projected_names(shared, "MemoryKeeper"), [])
        astro_detail = GalleryService(self.db).get_detail(
            shared.file_id,
            service_name="AstroJournal",
        )
        self.assertEqual([tag.tag for tag in astro_detail.ai_tags], ["Dog"])
        rows, total = GalleryRepository(self.db).search(
            tag="Dog",
            service_name="AstroJournal",
        )
        self.assertEqual(total, 2)
        self.assertEqual({row[0].id for row in rows}, {astro_only.id, shared.id})

    def test_file_delete_tombstones_suppression_without_physical_coupling(self) -> None:
        item = self.file(("MemoryKeeper", "AstroJournal"))
        self.raw(item, "Dog")
        self.visibility.hide(item.file_id, "ai:dog", expected_revision=0)

        service = MemoryKeeperFileService(
            self.db,
            cleanup_service=AstroJournalFileCleanupService(
                self.db,
                service_name="MemoryKeeper",
            ),
        )
        result = service.delete(item.file_id)

        self.assertEqual(result.cleanup_status, "PRESERVED_OTHER_SERVICE")
        suppression = self.db.query(MemoryKeeperFileTagSuppression).one()
        self.assertTrue(suppression.deleted)
        self.assertEqual(suppression.revision, 2)
        self.assertFalse(self.db.get(CommonFile, item.id).deleted)

    def test_schema_openapi_and_postgresql_ddl_are_additive(self) -> None:
        initialize_database(self.engine)
        initialize_database(self.engine)
        inspector = inspect(self.engine)
        self.assertIn("mk_file_tag_suppressions", inspector.get_table_names())
        indexes = {
            item["name"] for item in inspector.get_indexes("mk_file_tag_suppressions")
        }
        self.assertIn("uq_mk_file_tag_suppressions_file_canonical", indexes)
        foreign_keys = inspector.get_foreign_keys("mk_file_tag_suppressions")
        self.assertTrue(any(item["referred_table"] == "common_files" for item in foreign_keys))
        ddl = str(
            CreateTable(MemoryKeeperFileTagSuppression.__table__).compile(
                dialect=postgresql.dialect()
            )
        )
        self.assertIn("FOREIGN KEY(file_id) REFERENCES common_files", ddl)
        schema = app.openapi()
        path = "/api/memorykeeper/files/{file_id}/tags/catalog/{identity}"
        self.assertIn(path, schema["paths"])
        self.assertEqual(set(schema["paths"][path]), {"post", "delete"})
        response_properties = schema["components"]["schemas"][
            "FileTagVisibilityMutationResponse"
        ]["properties"]
        self.assertEqual(
            set(response_properties),
            {"file_id", "identity", "hidden", "revision"},
        )


if __name__ == "__main__":
    unittest.main()

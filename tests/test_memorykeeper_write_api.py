from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from sqlalchemy.orm import sessionmaker

from app.astrojournal.services.file_cleanup_service import (
    AstroJournalFileCleanupService,
    FileCleanupStatus,
)
from app.astrojournal.models.observation_record import ObservationRecord
from app.common.database import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.models.metadata_history import CommonMetadataHistory
from app.common.repositories.tag_repository import TagRepository
from app.common.services.gallery_service import GalleryService
from app.common.services.storage_service import StorageService
from app.common.schema_sync import initialize_database
from app.main import app
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.models.tag import Tag
from app.memorykeeper.schemas.file import MemoryKeeperFileMetadataUpdate
from app.memorykeeper.schemas.pending import PendingAssignPlaceRequest
from app.memorykeeper.schemas.place import FilePlaceUpdate, PlaceCreate
from app.memorykeeper.schemas.tag import TagCreate, TagMergeRequest, TagUpdate
from app.memorykeeper.services.file_service import MemoryKeeperFileService
from app.memorykeeper.services.pending_service import MemoryKeeperPendingService
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService
from app.memorykeeper.services.tag_service import MemoryKeeperTagService


class LocalStorageService(StorageService):
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def storage_root(self) -> Path:
        return self.root

    @property
    def original_root(self) -> Path:
        return self.root / "original"

    @property
    def preview_root(self) -> Path:
        return self.root / "preview"

    @property
    def thumb_root(self) -> Path:
        return self.root / "thumb"


class MemoryKeeperWriteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = LocalStorageService(Path(self.temp.name) / "PhotoPlatform")
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.cleanup = AstroJournalFileCleanupService(
            self.db,
            storage_service=self.storage,
            service_name="MemoryKeeper",
        )
        self.files = MemoryKeeperFileService(self.db, cleanup_service=self.cleanup)
        self.tags = MemoryKeeperTagService(self.db)
        self.places = MemoryKeeperPlaceService(self.db)
        self.pending = MemoryKeeperPendingService(self.db)
        self.counter = 0

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def file(
        self,
        *,
        services: tuple[str, ...] = ("MemoryKeeper",),
        gps: bool = True,
        place: MemoryKeeperPlace | None = None,
        physical: bool = False,
    ) -> tuple[CommonFile, CommonFileMetadata, dict[str, Path]]:
        self.counter += 1
        digest = f"{self.counter:064x}"
        paths = {
            "original": self.storage.original_root / "MemoryKeeper" / f"{digest}.jpg",
            "preview": self.storage.preview_root / digest[:2] / digest[2:4] / f"{digest}.jpg",
            "thumb": self.storage.thumb_root / digest[:2] / digest[2:4] / f"{digest}.jpg",
        }
        if physical:
            for kind, path in paths.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(kind.encode())
        common_file = CommonFile(
            file_id=digest,
            original_name=f"{digest}.jpg",
            service_name=services[0],
            original_path=self.storage.to_relative_path(paths["original"]) if physical else None,
            preview_path=self.storage.to_relative_path(paths["preview"]) if physical else None,
            thumb_path=self.storage.to_relative_path(paths["thumb"]) if physical else None,
            favorite=False,
            deleted=False,
        )
        self.db.add(common_file)
        self.db.flush()
        for service in services:
            self.db.add(CommonFileService(file_id=common_file.id, service_name=service))
        metadata = CommonFileMetadata(
            file_id=common_file.id,
            gps_lat=37.5 if gps else None,
            gps_lon=127.0 if gps else None,
            country="대한민국",
            province="서울특별시",
            city="서울",
            district="종로구",
            place_name="원시 주소",
            memorykeeper_place_id=place.id if place else None,
            place_match_source="USER" if place else None,
            place_match_revision=1 if place else 0,
        )
        self.db.add(metadata)
        self.db.commit()
        return common_file, metadata, paths

    def place(self, name: str = "서울숲", *, lat: float = 37.5, lon: float = 127.0):
        return self.places.create(
            PlaceCreate(
                display_name=name,
                canonical_name=name,
                latitude=lat,
                longitude=lon,
                radius_m=300,
            )
        )

    def test_memorykeeper_only_delete_cleans_assets_and_gallery(self) -> None:
        common_file, metadata, paths = self.file(physical=True)
        state = MemoryKeeperFileState(file_id=common_file.id, favorite=True, memo="memo")
        self.db.add(state)
        self.db.add(
            CommonFileTag(
                file_id=common_file.id,
                tag="user tag",
                tag_type="USER",
                source="USER",
                deleted=False,
            )
        )
        self.db.commit()

        result = self.files.delete(common_file.file_id)

        self.assertEqual(result.cleanup_status, FileCleanupStatus.CLEANED)
        self.assertTrue(result.physical_file_deleted)
        self.assertTrue(all(not path.exists() for path in paths.values()))
        self.db.refresh(common_file)
        self.assertTrue(common_file.deleted)
        self.assertEqual(GalleryService(self.db).list_gallery().total, 0)
        event = (
            self.db.query(CommonChangeEvent)
            .filter_by(resource_type="MemoryKeeperFile")
            .one()
        )
        self.assertEqual(event.operation, "DELETE")
        self.assertTrue(event.tombstone)
        self.assertIsNone(self.db.get(MemoryKeeperFileState, common_file.id))

    def test_shared_delete_unlinks_only_memorykeeper_and_preserves_raw_assets(self) -> None:
        place = self.place()
        common_file, metadata, paths = self.file(
            services=("MemoryKeeper", "AstroJournal"),
            place=place,
            physical=True,
        )
        self.db.add(MemoryKeeperFileState(file_id=common_file.id, favorite=True, memo="private"))
        ai = CommonFileTag(file_id=common_file.id, tag="sky", tag_type="AI", source="AI", deleted=False)
        user = CommonFileTag(file_id=common_file.id, tag="trip", tag_type="USER", source="USER", deleted=False)
        astro_record = ObservationRecord(
            file_id=common_file.id,
            service_name="AstroJournal",
            captured_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
        self.db.add_all([ai, user, astro_record])
        self.db.commit()

        result = self.files.delete(common_file.file_id)

        self.assertEqual(result.cleanup_status, FileCleanupStatus.PRESERVED_OTHER_SERVICE)
        self.assertTrue(all(path.exists() for path in paths.values()))
        links = {row.service_name for row in self.db.query(CommonFileService).all()}
        self.assertEqual(links, {"AstroJournal"})
        self.db.refresh(metadata)
        self.db.refresh(ai)
        self.db.refresh(user)
        self.assertEqual(metadata.gps_lat, 37.5)
        self.assertEqual(metadata.place_name, "원시 주소")
        self.assertIsNone(metadata.memorykeeper_place_id)
        self.assertFalse(ai.deleted)
        self.assertTrue(user.deleted)
        self.assertIsNone(self.db.get(MemoryKeeperFileState, common_file.id))
        with self.assertRaises(HTTPException) as raised:
            self.files.delete(common_file.file_id)
        self.assertEqual(raised.exception.status_code, 404)

    def test_delete_rejects_unsafe_path_and_rolls_back_unlink(self) -> None:
        common_file, _metadata, paths = self.file(physical=True)
        outside = Path(self.temp.name) / "outside.jpg"
        outside.write_bytes(b"keep")
        common_file.original_path = str(outside.resolve())
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            self.files.delete(common_file.file_id)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertTrue(outside.exists())
        self.assertTrue(paths["preview"].exists())
        self.assertEqual(self.db.query(CommonFileService).count(), 1)
        self.assertEqual(
            self.db.query(CommonChangeEvent).filter_by(resource_type="MemoryKeeperFile").count(),
            0,
        )

    def test_astro_only_file_is_rejected_by_all_memorykeeper_file_mutations(self) -> None:
        common_file, _metadata, _ = self.file(services=("AstroJournal",))
        with self.assertRaises(HTTPException) as raised:
            self.files.patch_metadata(
                common_file.file_id,
                MemoryKeeperFileMetadataUpdate(expected_revision=0, favorite=True),
            )
        self.assertEqual(raised.exception.status_code, 404)
        with self.assertRaises(HTTPException) as raised:
            self.files.delete(common_file.file_id)
        self.assertEqual(raised.exception.status_code, 404)

    def test_metadata_patch_favorite_memo_clear_history_change_and_gallery(self) -> None:
        common_file, _metadata, _ = self.file()
        response = self.files.patch_metadata(
            common_file.file_id,
            MemoryKeeperFileMetadataUpdate(
                expected_revision=0,
                favorite=True,
                memo="첫 여행",
            ),
        )
        self.assertTrue(response.favorite)
        self.assertEqual(response.memo, "첫 여행")
        self.assertEqual(response.revision, 1)
        detail = GalleryService(self.db).get_detail(common_file.file_id, service_name="MemoryKeeper")
        self.assertTrue(detail.favorite)
        self.assertEqual(detail.memo, "첫 여행")
        self.assertEqual(detail.metadata_revision, 1)
        fields = {
            row.field_name
            for row in self.db.query(CommonMetadataHistory).filter_by(file_id=common_file.id)
        }
        self.assertEqual(fields, {"memorykeeper_favorite", "memorykeeper_memo"})

        cleared = self.files.patch_metadata(
            common_file.file_id,
            MemoryKeeperFileMetadataUpdate(
                expected_revision=1,
                favorite=False,
                memo="",
            ),
        )
        self.assertFalse(cleared.favorite)
        self.assertIsNone(cleared.memo)
        self.assertEqual(cleared.revision, 2)
        self.assertEqual(
            self.db.query(CommonChangeEvent).filter_by(resource_type="MemoryKeeperFileMetadata").count(),
            2,
        )

    def test_metadata_revision_conflict_has_current_revision(self) -> None:
        common_file, _metadata, _ = self.file()
        self.files.patch_metadata(
            common_file.file_id,
            MemoryKeeperFileMetadataUpdate(expected_revision=0, favorite=True),
        )
        with self.assertRaises(HTTPException) as raised:
            self.files.patch_metadata(
                common_file.file_id,
                MemoryKeeperFileMetadataUpdate(expected_revision=0, memo="stale"),
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["current_revision"], 1)

    def test_location_patch_preserves_user_place_and_shared_raw_metadata(self) -> None:
        place = self.place(lat=37.0, lon=127.0)
        common_file, metadata, _ = self.file(
            services=("MemoryKeeper", "AstroJournal"),
            place=place,
        )
        response = self.files.patch_metadata(
            common_file.file_id,
            MemoryKeeperFileMetadataUpdate(
                expected_revision=0,
                gps_lat=37.01,
                gps_lon=127.01,
                district="중구",
                place_name=None,
            ),
        )
        self.assertEqual(str(response.memorykeeper_place_id), place.id)
        self.assertEqual(response.place_match_source, "USER")
        self.assertGreater(response.place_match_distance_m, 0)
        self.db.refresh(metadata)
        self.assertEqual(metadata.gps_lat, 37.01)
        self.assertIsNone(metadata.place_name)
        astro = GalleryService(self.db).get_detail(common_file.file_id, service_name="AstroJournal")
        self.assertFalse(astro.favorite)
        self.assertIsNone(astro.memo)
        self.assertEqual(astro.metadata["gps_lat"], 37.01)

    def test_location_patch_reclassifies_only_automatic_place(self) -> None:
        place = self.place(lat=37.5, lon=127.0)
        common_file, metadata, _ = self.file(place=place)
        metadata.place_match_source = "RADIUS"
        self.db.commit()
        response = self.files.patch_metadata(
            common_file.file_id,
            MemoryKeeperFileMetadataUpdate(
                expected_revision=0,
                gps_lat=35.0,
                gps_lon=129.0,
            ),
        )
        self.assertIsNone(response.memorykeeper_place_id)
        self.assertEqual(response.place_match_source, "AUTO_PLACE_MATCH")

    def test_tag_crud_assign_usage_remove_and_vision_tombstone(self) -> None:
        common_file, _metadata, _ = self.file()
        ai = CommonFileTag(file_id=common_file.id, tag="바다", tag_type="AI", source="AI", deleted=False)
        self.db.add(ai)
        self.db.commit()
        tag = self.tags.create(TagCreate(name="바다"))
        with self.assertRaises(HTTPException) as raised:
            self.tags.create(TagCreate(name="  바다  "))
        self.assertEqual(raised.exception.status_code, 409)

        assigned = self.tags.assign(common_file.file_id, tag.id, expected_revision=0)
        self.assertTrue(assigned.assigned)
        self.db.refresh(ai)
        self.assertFalse(ai.deleted)
        self.assertEqual(self.tags.list(query=None, favorite=None, limit=10, offset=0).items[0].usage_count, 1)
        detail = GalleryService(self.db).get_detail(common_file.file_id, service_name="MemoryKeeper")
        self.assertEqual(detail.user_tags[0].tag_id, tag.id)

        renamed = self.tags.update(tag.id, TagUpdate(revision=1, name="푸른 바다", favorite=True))
        self.assertEqual(renamed.name, "푸른 바다")
        removed = self.tags.remove(common_file.file_id, tag.id, expected_revision=1)
        self.assertFalse(removed.assigned)
        self.assertEqual(removed.revision, 2)
        preserved_raw = TagRepository(self.db).save_ai_tag(
            file_id=common_file.id,
            tag="푸른 바다",
        )
        self.assertIsNotNone(preserved_raw)
        self.assertEqual(preserved_raw.source, "AI")
        self.assertEqual(self.tags.list(query=None, favorite=None, limit=10, offset=0).items[0].usage_count, 0)
        resource_types = {
            event.resource_type for event in self.db.query(CommonChangeEvent).all()
        }
        self.assertTrue({"MemoryKeeperTag", "MemoryKeeperFileTag"} <= resource_types)

    def test_tag_merge_moves_relations_without_duplicates_and_delete_tombstones(self) -> None:
        first_file, _metadata, _ = self.file()
        second_file, _metadata, _ = self.file()
        source = self.tags.create(TagCreate(name="여행"))
        target = self.tags.create(TagCreate(name="트립"))
        self.tags.assign(first_file.file_id, source.id, expected_revision=0)
        self.tags.assign(first_file.file_id, target.id, expected_revision=1)
        self.tags.assign(second_file.file_id, source.id, expected_revision=0)

        merged = self.tags.merge(
            source.id,
            TagMergeRequest(
                source_revision=source.revision,
                target_tag_id=target.id,
                target_revision=target.revision,
            ),
        )
        self.assertEqual(merged.usage_count, 2)
        self.assertTrue(self.db.get(Tag, source.id).deleted)
        active_target = (
            self.db.query(CommonFileTag)
            .filter_by(memorykeeper_tag_id=target.id, deleted=False)
            .count()
        )
        self.assertEqual(active_target, 2)

        self.tags.delete(target.id, expected_revision=merged.revision)
        self.assertTrue(self.db.get(Tag, target.id).deleted)
        self.assertEqual(
            self.db.query(CommonFileTag).filter_by(memorykeeper_tag_id=target.id, deleted=False).count(),
            0,
        )

    def test_pending_is_derived_from_place_relation_and_gallery_count_matches(self) -> None:
        place = self.place()
        pending_file, pending_metadata, _ = self.file()
        assigned_file, _assigned_metadata, _ = self.file(place=place)
        listed = self.pending.list(page=1, page_size=20)
        self.assertEqual(listed.total, 1)
        self.assertEqual(listed.items[0].file_id, pending_file.file_id)
        self.assertEqual(listed.items[0].place_name, "원시 주소")
        gallery = GalleryService(self.db).list_gallery(incomplete=True)
        self.assertEqual(gallery.total, listed.total)

        self.places.assign_file(
            public_file_id=pending_file.file_id,
            place_id=place.id,
            expected_revision=0,
        )
        self.assertEqual(self.pending.list(page=1, page_size=20).total, 0)
        self.places.assign_file(
            public_file_id=pending_file.file_id,
            place_id=None,
            expected_revision=1,
        )
        self.db.refresh(pending_metadata)
        self.assertIsNone(pending_metadata.memorykeeper_place_id)
        self.assertEqual(self.pending.list(page=1, page_size=20).total, 1)
        self.assertFalse(
            any(item.file_id == assigned_file.file_id for item in self.pending.list(page=1, page_size=20).items)
        )

    def test_pending_batch_assign_is_all_or_nothing_and_supports_shared_file(self) -> None:
        place = self.place()
        first, first_metadata, _ = self.file()
        shared, shared_metadata, _ = self.file(services=("AstroJournal", "MemoryKeeper"))
        astro, _astro_metadata, _ = self.file(services=("AstroJournal",))
        invalid_payload = PendingAssignPlaceRequest(
            file_ids=[first.file_id, astro.file_id],
            memorykeeper_place_id=place.id,
            expected_revisions={first.file_id: 0, astro.file_id: 0},
        )
        with self.assertRaises(HTTPException) as raised:
            self.pending.assign_place(invalid_payload)
        self.assertEqual(raised.exception.status_code, 404)
        self.db.refresh(first_metadata)
        self.assertIsNone(first_metadata.memorykeeper_place_id)

        result = self.pending.assign_place(
            PendingAssignPlaceRequest(
                file_ids=[first.file_id, shared.file_id],
                memorykeeper_place_id=place.id,
                expected_revisions={first.file_id: 0, shared.file_id: 0},
            )
        )
        self.assertEqual(result.assigned_count, 2)
        self.db.refresh(first_metadata)
        self.db.refresh(shared_metadata)
        self.assertEqual(first_metadata.memorykeeper_place_id, place.id)
        self.assertEqual(shared_metadata.memorykeeper_place_id, place.id)
        self.assertEqual(first_metadata.gps_lat, 37.5)
        self.assertEqual(shared_metadata.place_name, "원시 주소")
        self.assertEqual(
            self.db.query(CommonChangeEvent)
            .filter_by(resource_type="MemoryKeeperFilePlace")
            .count(),
            2,
        )

    def test_new_routes_are_bearer_protected(self) -> None:
        paths = app.openapi()["paths"]
        expected = {
            "/api/memorykeeper/files/{file_id}": "delete",
            "/api/memorykeeper/files/{file_id}/metadata": "patch",
            "/api/memorykeeper/tags": "get",
            "/api/memorykeeper/tags/{tag_id}/merge": "post",
            "/api/memorykeeper/files/{file_id}/tags/{tag_id}": "post",
            "/api/memorykeeper/pending": "get",
            "/api/memorykeeper/pending/assign-place": "post",
        }
        for path, method in expected.items():
            self.assertTrue(paths[path][method]["security"], path)

    def test_schema_sync_upgrades_legacy_tag_tables_and_postgres_ddl_compiles(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.execute(text("""
                    CREATE TABLE mk_tags (
                        id INTEGER PRIMARY KEY,
                        tag_name VARCHAR(100) NOT NULL UNIQUE,
                        tag_type VARCHAR(50) NOT NULL,
                        source VARCHAR(50) NOT NULL,
                        created_at DATETIME
                    )
                """))
                connection.execute(text("""
                    CREATE TABLE common_file_tags (
                        id INTEGER PRIMARY KEY,
                        file_id INTEGER NOT NULL,
                        tag VARCHAR(255) NOT NULL,
                        tag_type VARCHAR(20) NOT NULL,
                        source VARCHAR(20) NOT NULL,
                        confidence FLOAT,
                        created_at DATETIME,
                        updated_at DATETIME,
                        deleted BOOLEAN NOT NULL DEFAULT 0
                    )
                """))
            changes = initialize_database(engine)
            inspector = inspect(engine)
            self.assertTrue(inspector.has_table("memorykeeper_file_states"))
            tag_columns = {column["name"] for column in inspector.get_columns("mk_tags")}
            self.assertTrue({"normalized_name", "favorite", "revision", "deleted", "updated_at"} <= tag_columns)
            relation_columns = {
                column["name"] for column in inspector.get_columns("common_file_tags")
            }
            self.assertIn("memorykeeper_tag_id", relation_columns)
            indexes = {item["name"] for item in inspector.get_indexes("common_file_tags")}
            self.assertIn("uq_common_file_tags_memorykeeper_relation", indexes)
            self.assertTrue(any("mk_tags.normalized_name" in item for item in changes))

            ddl = str(
                CreateTable(MemoryKeeperFileState.__table__).compile(
                    dialect=postgresql.dialect()
                )
            )
            self.assertIn("memorykeeper_file_states", ddl)
            self.assertIn("BOOLEAN", ddl)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()

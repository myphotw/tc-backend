from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.metadata_history import CommonMetadataHistory
from app.common.schema_sync import initialize_database
from app.common.repositories.geocode_cache_repository import GeocodeCacheRepository
from app.common.services.gallery_service import GalleryService
from app.common.utils.perf import QueryCounter
from app.main import app
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.schemas.place import (
    PlaceCreate,
    PlaceMatchRequest,
    PlaceUpdate,
    RadiusImpactRequest,
)
from app.memorykeeper.services.place_matcher import MemoryKeeperPlaceMatcher
from app.memorykeeper.services.place_candidate_service import (
    MemoryKeeperPlaceCandidateService,
)
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService
from scripts.backfill_memorykeeper_places import backfill_memorykeeper_places
from scripts.migrate_memorykeeper_places import migrate_rows
from worker.plugins.base import PluginContext
from worker.plugins.gps_plugin import GpsPlugin


class MemoryKeeperPlaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.service = MemoryKeeperPlaceService(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def place(self, name: str = "피아골", **values) -> MemoryKeeperPlace:
        payload = {
            "display_name": name,
            "canonical_name": name.casefold(),
            "latitude": 35.2274227,
            "longitude": 127.5905236,
            "radius_m": 500.0,
            **values,
        }
        return self.service.create(PlaceCreate(**payload))

    def file(self, marker: str, *, services=("MemoryKeeper",), lat=35.2274227, lon=127.5905236):
        common_file = CommonFile(file_id=marker * 64, original_name=f"{marker}.jpg", service_name=services[0])
        self.db.add(common_file)
        self.db.flush()
        for service in services:
            self.db.add(CommonFileService(file_id=common_file.id, service_name=service))
        metadata = CommonFileMetadata(
            file_id=common_file.id,
            gps_lat=lat,
            gps_lon=lon,
            country="대한민국",
            province="전라남도",
            district="구례군",
            place_name="원시 역지오코딩 주소",
        )
        self.db.add(metadata)
        self.db.commit()
        return common_file, metadata

    def auto_service(self, nearby_items: list[dict[str, object]]) -> MemoryKeeperPlaceService:
        candidates = MemoryKeeperPlaceCandidateService(
            self.db,
            nearby_lookup=lambda _lat, _lon, _radius: nearby_items,
        )
        return MemoryKeeperPlaceService(self.db, candidate_service=candidates)

    def test_crud_filters_sort_and_revision_conflict(self) -> None:
        first = self.place(favorite=False)
        favorite = self.place("난바", favorite=True, latitude=34.6, longitude=135.5)
        listed = self.service.list(active=True, favorite=None, query=None, limit=10, offset=0)
        self.assertEqual([str(item.id) for item in listed.items], [favorite.id, first.id])
        updated = self.service.update(first.id, PlaceUpdate(revision=1, display_name="지리산 피아골"))
        self.assertEqual(updated.display_name, "지리산 피아골")
        self.assertEqual(updated.canonical_name, "피아골")
        self.assertEqual(updated.revision, 2)
        with self.assertRaisesRegex(Exception, "REVISION_CONFLICT"):
            self.service.update(first.id, PlaceUpdate(revision=1, favorite=True))

    def test_validation(self) -> None:
        for values in (
            {"display_name": " ", "latitude": 0, "longitude": 0, "radius_m": 1},
            {"display_name": "x", "latitude": 91, "longitude": 0, "radius_m": 1},
            {"display_name": "x", "latitude": 0, "longitude": 181, "radius_m": 1},
            {"display_name": "x", "latitude": 0, "longitude": 0, "radius_m": 0},
        ):
            with self.assertRaises(ValidationError):
                PlaceCreate(**values)

    def test_provider_then_canonical_precedence(self) -> None:
        provider = self.place("Provider", provider_place_id="g-1", latitude=35.3)
        canonical = self.place("Canonical", canonical_name="same", latitude=35.2)
        match = self.service.match(PlaceMatchRequest(latitude=35.2, longitude=127.59, provider_place_id="g-1", canonical_name="same"))
        self.assertEqual(str(match.place.id), provider.id)
        self.assertEqual(match.match_source, "PROVIDER_PLACE_ID")
        match = self.service.match(PlaceMatchRequest(latitude=35.2, longitude=127.59, canonical_name=" SAME "))
        self.assertEqual(str(match.place.id), canonical.id)
        self.assertEqual(match.match_source, "CANONICAL_NAME")

    def test_radius_chooses_nearest_and_excludes_inactive(self) -> None:
        far = self.place("Far", latitude=35.228, radius_m=1000)
        near = self.place("Near", latitude=35.2275, radius_m=1000)
        near.active = False
        self.db.commit()
        match = self.service.match(PlaceMatchRequest(latitude=35.2275, longitude=127.5905236))
        self.assertEqual(str(match.place.id), far.id)
        self.assertEqual(match.match_source, "RADIUS")

    def test_radius_outside_is_no_match(self) -> None:
        self.place(radius_m=10)
        match = self.service.match(PlaceMatchRequest(latitude=36.0, longitude=128.0))
        self.assertFalse(match.matched)
        self.assertEqual(match.match_source, "NONE")

    def test_equal_distance_tie_breaks_by_id(self) -> None:
        first = MemoryKeeperPlace(id="00000000-0000-0000-0000-000000000001", display_name="A", latitude=35, longitude=127, radius_m=1000)
        second = MemoryKeeperPlace(id="00000000-0000-0000-0000-000000000002", display_name="B", latitude=35, longitude=127, radius_m=1000)
        self.db.add_all([second, first])
        self.db.commit()
        match = MemoryKeeperPlaceMatcher(self.db).match(gps_lat=35, gps_lon=127)
        self.assertEqual(match.place.id, first.id)

    def test_auto_match_only_for_memorykeeper_link_and_shared_file(self) -> None:
        place = self.place()
        mk_file, mk_metadata = self.file("a")
        astro_file, astro_metadata = self.file("b", services=("AstroJournal",))
        shared_file, shared_metadata = self.file("c", services=("AstroJournal", "MemoryKeeper"))
        self.assertTrue(self.service.auto_match_file(file_id=mk_file.id))
        self.assertFalse(self.service.auto_match_file(file_id=astro_file.id))
        self.assertTrue(self.service.auto_match_file(file_id=shared_file.id))
        self.db.refresh(mk_metadata)
        self.db.refresh(astro_metadata)
        self.db.refresh(shared_metadata)
        self.assertEqual(mk_metadata.memorykeeper_place_id, place.id)
        self.assertIsNone(astro_metadata.memorykeeper_place_id)
        self.assertEqual(shared_metadata.memorykeeper_place_id, place.id)

    def test_auto_create_prefers_meaningful_poi_over_nearest_generic_result(self) -> None:
        common_file, metadata = self.file("m")
        service = self.auto_service(
            [
                {
                    "place_id": "bridge-1",
                    "place_name": "원기교",
                    "latitude": 35.2274227,
                    "longitude": 127.5905236,
                    "types": ["point_of_interest", "establishment"],
                    "rating": 5,
                    "user_ratings_total": 1000,
                },
                {
                    "place_id": "valley-1",
                    "place_name": "피아골",
                    "latitude": 35.232,
                    "longitude": 127.5905236,
                    "types": ["natural_feature", "point_of_interest"],
                },
            ]
        )
        self.assertTrue(service.auto_match_file(file_id=common_file.id))
        self.db.refresh(metadata)
        place = self.db.get(MemoryKeeperPlace, metadata.memorykeeper_place_id)
        self.assertEqual(place.display_name, "피아골")
        self.assertEqual(place.address, "원시 역지오코딩 주소")
        self.assertEqual(place.category, "NATURE")
        self.assertEqual(place.radius_m, 200.0)
        self.assertEqual(place.creation_source, "AUTO_POI")
        self.assertEqual(metadata.place_match_source, "AUTO_CREATED")

    def test_auto_create_falls_back_to_locality_and_preserves_raw_metadata(self) -> None:
        common_file, metadata = self.file("n")
        service = self.auto_service([])
        self.assertTrue(service.auto_match_file(file_id=common_file.id))
        self.db.refresh(metadata)
        place = self.db.get(MemoryKeeperPlace, metadata.memorykeeper_place_id)
        self.assertEqual(place.display_name, "구례군")
        self.assertEqual(place.address, "원시 역지오코딩 주소")
        self.assertEqual(place.creation_source, "AUTO_LOCALITY")
        self.assertEqual(metadata.gps_lat, 35.2274227)
        self.assertEqual(metadata.gps_lon, 127.5905236)
        self.assertEqual(metadata.place_name, "원시 역지오코딩 주소")

    def test_auto_candidate_reuses_existing_provider_and_canonical_places(self) -> None:
        provider = self.place(
            "기존 Provider",
            provider_place_id="provider-same",
            latitude=36.0,
            longitude=128.0,
            radius_m=10,
        )
        first_file, first_metadata = self.file("o")
        provider_service = self.auto_service(
            [{
                "place_id": "provider-same",
                "place_name": "새 Provider 이름",
                "latitude": 35.2274227,
                "longitude": 127.5905236,
                "types": ["tourist_attraction"],
            }]
        )
        before = self.db.query(MemoryKeeperPlace).count()
        provider_service.auto_match_file(file_id=first_file.id)
        self.db.refresh(first_metadata)
        self.assertEqual(first_metadata.memorykeeper_place_id, provider.id)
        self.assertEqual(first_metadata.place_match_source, "PROVIDER_PLACE_ID")
        self.assertEqual(self.db.query(MemoryKeeperPlace).count(), before)

        canonical = self.place(
            "Canonical Place",
            canonical_name="canonical-poi",
            latitude=36.1,
            longitude=128.1,
            radius_m=10,
        )
        second_file, second_metadata = self.file("p")
        canonical_service = self.auto_service(
            [{
                "place_id": "new-provider",
                "place_name": "canonical-poi",
                "latitude": 35.2274227,
                "longitude": 127.5905236,
                "types": ["park"],
            }]
        )
        canonical_service.auto_match_file(file_id=second_file.id)
        self.db.refresh(second_metadata)
        self.assertEqual(second_metadata.memorykeeper_place_id, canonical.id)
        self.assertEqual(second_metadata.place_match_source, "CANONICAL_NAME")
        self.assertEqual(self.db.query(MemoryKeeperPlace).count(), before + 1)

    def test_multiple_unmatched_files_create_one_place_and_unique_key_guards_race(self) -> None:
        first, first_metadata = self.file("q")
        second, second_metadata = self.file("r", lat=35.22745, lon=127.59055)
        service = self.auto_service([])
        service.auto_match_file(file_id=first.id)
        service.auto_match_file(file_id=second.id)
        self.db.refresh(first_metadata)
        self.db.refresh(second_metadata)
        self.assertEqual(first_metadata.memorykeeper_place_id, second_metadata.memorykeeper_place_id)
        self.assertEqual(self.db.query(MemoryKeeperPlace).count(), 1)

        self.db.add(
            MemoryKeeperPlace(
                display_name="Duplicate",
                latitude=0,
                longitude=0,
                radius_m=200,
                auto_dedup_key=self.db.query(MemoryKeeperPlace).one().auto_dedup_key,
            )
        )
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

        existing = self.db.query(MemoryKeeperPlace).one()
        candidate = service.build_auto_candidate(first_metadata)
        with patch.object(
            service,
            "_candidate_duplicate",
            return_value=(None, "NONE"),
        ):
            resolved, created, _ = service._resolve_or_create_candidate(
                candidate,
                photo_lat=float(first_metadata.gps_lat),
                photo_lon=float(first_metadata.gps_lon),
            )
        self.assertFalse(created)
        self.assertEqual(resolved.id, existing.id)

    def test_astro_only_does_not_auto_create_but_shared_file_does(self) -> None:
        astro, astro_metadata = self.file("s", services=("AstroJournal",))
        shared, shared_metadata = self.file("t", services=("AstroJournal", "MemoryKeeper"))
        service = self.auto_service([])
        self.assertFalse(service.auto_match_file(file_id=astro.id))
        self.assertTrue(service.auto_match_file(file_id=shared.id))
        self.db.refresh(astro_metadata)
        self.db.refresh(shared_metadata)
        self.assertIsNone(astro_metadata.memorykeeper_place_id)
        self.assertIsNotNone(shared_metadata.memorykeeper_place_id)

    def test_gps_plugin_runs_auto_match_after_raw_geocoding(self) -> None:
        place = self.place(canonical_name="원시 역지오코딩 주소")
        common_file, metadata = self.file("l")
        GeocodeCacheRepository(self.db).save(
            latitude=35.2274227,
            longitude=127.5905236,
            country="대한민국",
            province="전라남도",
            city="구례군",
            district="토지면",
            place_name="원시 역지오코딩 주소",
            provider="GOOGLE",
        )
        context = PluginContext(
            db=self.db,
            storage_service=object(),  # unused by GpsPlugin
            common_file=common_file,
            has_gps=True,
            gps_lat=35.2274227,
            gps_lon=127.5905236,
        )
        GpsPlugin().run(context)
        self.db.refresh(metadata)
        self.assertEqual(metadata.memorykeeper_place_id, place.id)
        self.assertEqual(metadata.place_match_source, "CANONICAL_NAME")
        self.assertIn("MEMORYKEEPER_PLACE_MATCHED", context.processing_log)

    def test_manual_assign_and_unassign_preserve_raw_metadata_and_history(self) -> None:
        place = self.place()
        common_file, metadata = self.file("d")
        response = self.service.assign_file(public_file_id=common_file.file_id, place_id=place.id, expected_revision=0)
        self.assertEqual(str(response.memorykeeper_place_id), place.id)
        self.assertEqual(response.place_match_source, "USER")
        self.assertIsNotNone(response.place_match_distance_m)
        response = self.service.assign_file(public_file_id=common_file.file_id, place_id=None, expected_revision=1)
        self.assertIsNone(response.memorykeeper_place_id)
        self.assertFalse(self.service.auto_match_file(file_id=common_file.id))
        self.db.refresh(metadata)
        self.assertIsNone(metadata.memorykeeper_place_id)
        self.assertEqual(metadata.gps_lat, 35.2274227)
        self.assertEqual(metadata.place_name, "원시 역지오코딩 주소")
        self.assertGreater(self.db.query(CommonMetadataHistory).count(), 0)
        self.assertGreater(self.db.query(CommonChangeEvent).count(), 0)

    def test_display_name_change_is_immediate_in_gallery_and_astro_isolated(self) -> None:
        place = self.place()
        common_file, _ = self.file("e", services=("MemoryKeeper", "AstroJournal"))
        self.service.auto_match_file(file_id=common_file.id)
        memory = GalleryService(self.db).get_detail(common_file.file_id, service_name="MemoryKeeper")
        astro = GalleryService(self.db).get_detail(common_file.file_id, service_name="AstroJournal")
        self.assertEqual(memory.place_display_name, "피아골")
        self.assertIsNone(astro.place_display_name)
        self.service.update(place.id, PlaceUpdate(revision=1, display_name="지리산 피아골"))
        renamed = GalleryService(self.db).get_detail(common_file.file_id, service_name="MemoryKeeper")
        self.assertEqual(renamed.place_display_name, "지리산 피아골")

    def test_gallery_list_detail_map_additive_fields_and_raw_contract(self) -> None:
        place = self.place()
        common_file, _ = self.file("f")
        self.service.auto_match_file(file_id=common_file.id)
        gallery = GalleryService(self.db)
        item = gallery.list_gallery(service_name="MemoryKeeper").items[0]
        detail = gallery.get_detail(common_file.file_id, service_name="MemoryKeeper")
        marker = gallery.map_markers(service_name="MemoryKeeper").items[0]
        for value in (item.place_display_name, detail.place_display_name, marker.place_display_name):
            self.assertEqual(value, place.display_name)
        self.assertEqual(item.place_name, "원시 역지오코딩 주소")
        self.assertEqual(marker.latitude, 35.2274227)

    def test_gallery_map_is_one_projection_query_and_preserves_shape(self) -> None:
        place = self.place()
        for marker in ("q", "r", "s"):
            _, metadata = self.file(marker)
            metadata.memorykeeper_place_id = place.id
            metadata.place_match_source = "RADIUS"
            metadata.place_match_distance_m = 12.5
            metadata.place_match_revision = 3
        self.db.commit()
        self.db.expunge_all()

        with QueryCounter(self.engine) as counter:
            response = GalleryService(self.db).map_markers(
                service_name="MemoryKeeper"
            )

        self.assertEqual(counter.count, 1)
        self.assertEqual(response.total, 3)
        marker = response.items[0]
        self.assertEqual(
            set(marker.model_dump()),
            {
                "file_id",
                "latitude",
                "longitude",
                "place_name",
                "geocoded_place_name",
                "memorykeeper_place_id",
                "place_display_name",
                "place_canonical_name",
                "place_match_source",
                "place_match_distance_m",
                "place_revision",
                "province",
                "district",
                "thumbnail",
                "year",
                "service_name",
            },
        )
        self.assertEqual(marker.memorykeeper_place_id, place.id)
        self.assertEqual(marker.place_display_name, "피아골")
        self.assertEqual(marker.geocoded_place_name, "원시 역지오코딩 주소")
        self.assertEqual(marker.place_revision, 3)

    def test_gallery_map_filters_service_gps_deleted_and_year_without_duplicates(
        self,
    ) -> None:
        captured_2024 = datetime(2024, 6, 1, tzinfo=timezone.utc)
        captured_2024_newer = datetime(2024, 6, 2, tzinfo=timezone.utc)
        captured_2023 = datetime(2023, 6, 1, tzinfo=timezone.utc)

        memory_file, memory_metadata = self.file("t")
        memory_metadata.datetime_original = captured_2024_newer
        shared_file, shared_metadata = self.file(
            "u",
            services=("MemoryKeeper", "AstroJournal"),
        )
        shared_metadata.datetime_original = captured_2024
        _, astro_metadata = self.file("v", services=("AstroJournal",))
        astro_metadata.datetime_original = captured_2024
        _, no_gps_metadata = self.file("w", lat=None, lon=None)
        no_gps_metadata.datetime_original = captured_2024
        deleted_file, deleted_metadata = self.file("x")
        deleted_file.deleted = True
        deleted_metadata.datetime_original = captured_2024
        _, old_metadata = self.file("y")
        old_metadata.datetime_original = captured_2023
        self.db.commit()

        response = GalleryService(self.db).map_markers(
            service_name="MemoryKeeper",
            year=2024,
        )

        self.assertEqual(response.total, 2)
        self.assertEqual(
            [item.file_id for item in response.items],
            [memory_file.file_id, shared_file.file_id],
        )
        self.assertEqual(
            {item.file_id for item in response.items},
            {memory_file.file_id, shared_file.file_id},
        )
        self.assertEqual(len({item.file_id for item in response.items}), 2)

        astro_response = GalleryService(self.db).map_markers(
            service_name="AstroJournal",
            year=2024,
        )
        self.assertEqual(
            {item.file_id for item in astro_response.items},
            {shared_file.file_id, "v" * 64},
        )
        self.assertTrue(
            all(item.service_name == "AstroJournal" for item in astro_response.items)
        )

    def test_delete_nulls_relation_but_preserves_raw(self) -> None:
        place = self.place()
        common_file, metadata = self.file("g")
        self.service.auto_match_file(file_id=common_file.id)
        self.service.delete(place.id)
        self.db.refresh(metadata)
        self.assertIsNone(metadata.memorykeeper_place_id)
        self.assertEqual(metadata.place_name, "원시 역지오코딩 주소")
        self.assertEqual(metadata.gps_lon, 127.5905236)

    def test_reclassify_unassigned_reassign_option_and_outside_unlink(self) -> None:
        target = self.place(radius_m=200)
        other = self.place("Other", latitude=35.4, longitude=127.9, radius_m=10)
        unassigned, _ = self.file("h")
        assigned, assigned_metadata = self.file("i")
        self.service.assign_file(public_file_id=assigned.file_id, place_id=other.id, expected_revision=0)
        result = self.service.reclassify(target.id, reassign_from_other_places=False)
        self.assertEqual(result.assigned, 1)
        self.db.refresh(assigned_metadata)
        self.assertEqual(assigned_metadata.memorykeeper_place_id, other.id)
        result = self.service.reclassify(target.id, reassign_from_other_places=True)
        self.assertEqual(result.reassigned, 1)
        target.latitude = 36
        target.longitude = 128
        self.db.commit()
        result = self.service.reclassify(target.id, reassign_from_other_places=False)
        self.assertEqual(result.unassigned_outside_radius, 2)

    def test_radius_impact_counts_files_and_overlaps(self) -> None:
        self.place("Overlap", latitude=35.2275, radius_m=300)
        self.file("j")
        impact = self.service.radius_impact(RadiusImpactRequest(latitude=35.2274227, longitude=127.5905236, radius_m=300))
        self.assertEqual(impact.matched_file_count, 1)
        self.assertEqual(len(impact.overlapping_places), 1)

    def test_dry_run_backfill_and_migration_do_not_write(self) -> None:
        self.place()
        self.file("k")
        before_relations = self.db.query(CommonFileMetadata).filter(CommonFileMetadata.memorykeeper_place_id.isnot(None)).count()
        stats = backfill_memorykeeper_places(self.db, execute=False)
        self.assertEqual(stats.matched, 1)
        self.assertEqual(self.db.query(CommonFileMetadata).filter(CommonFileMetadata.memorykeeper_place_id.isnot(None)).count(), before_relations)
        before_places = self.db.query(MemoryKeeperPlace).count()
        migration = migrate_rows(self.db, [{"Id": str(uuid4()), "DisplayName": "난바", "Latitude": 34.6, "Longitude": 135.5, "Radius": 100}], execute=False)
        self.assertEqual(migration.created, 1)
        self.assertEqual(self.db.query(MemoryKeeperPlace).count(), before_places)

    def test_create_missing_backfill_dry_run_does_not_write(self) -> None:
        self.file("u")
        before_places = self.db.query(MemoryKeeperPlace).count()
        stats = backfill_memorykeeper_places(
            self.db,
            execute=False,
            create_missing=True,
        )
        self.assertEqual(stats.would_create, 1)
        self.assertEqual(self.db.query(MemoryKeeperPlace).count(), before_places)
        metadata = self.db.query(CommonFileMetadata).one()
        self.assertIsNone(metadata.memorykeeper_place_id)

    def test_schema_sync_contains_table_relation_and_indexes(self) -> None:
        second_engine = create_engine("sqlite:///:memory:")
        try:
            # Simulate an existing pre-feature metadata table.
            with second_engine.begin() as connection:
                connection.execute(
                    text("CREATE TABLE common_file_metadata (id INTEGER PRIMARY KEY)")
                )
            initialize_database(second_engine)
            inspector = inspect(second_engine)
            self.assertTrue(inspector.has_table("memorykeeper_places"))
            columns = {item["name"] for item in inspector.get_columns("common_file_metadata")}
            self.assertIn("memorykeeper_place_id", columns)
            self.assertIn("place_match_revision", columns)
            place_indexes = {
                item["name"]: item
                for item in inspector.get_indexes("memorykeeper_places")
            }
            self.assertTrue(
                place_indexes["uq_memorykeeper_places_auto_dedup_key"]["unique"]
            )
        finally:
            second_engine.dispose()

    def test_openapi_exposes_place_endpoints_under_bearer_security(self) -> None:
        paths = app.openapi()["paths"]
        for path in (
            "/api/memorykeeper/places",
            "/api/memorykeeper/places/match",
            "/api/memorykeeper/places/radius-impact",
            "/api/memorykeeper/places/{place_id}/reclassify",
            "/api/memorykeeper/files/{file_id}/place",
        ):
            self.assertIn(path, paths)
        self.assertTrue(paths["/api/memorykeeper/places"]["get"]["security"])


if __name__ == "__main__":
    unittest.main()

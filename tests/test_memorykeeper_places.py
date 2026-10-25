from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
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
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.schemas.place import (
    PlaceCreate,
    PlaceMatchRequest,
    PlaceMergeRequest,
    PlaceUpdate,
    RadiusImpactRequest,
)
from app.memorykeeper.services.place_matcher import MemoryKeeperPlaceMatcher
from app.memorykeeper.services.place_candidate_service import (
    MemoryKeeperPlaceCandidateService,
)
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService
from app.memorykeeper.services.fast_gallery_service import MemoryKeeperFastGalleryService
from app.memorykeeper.repositories.place_cleanup_repository import (
    LOCATION_REASON_COUNTRY_MISSING,
    LOCATION_REASON_PLACE_MISSING,
    LOCATION_REASON_REGION_MISSING,
    location_hierarchy_reason,
)
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

    def test_place_hierarchy_reuses_gallery_paths_and_includes_empty_places(self) -> None:
        beach = self.place(
            "헐후메일 퍼블릭 비치",
            country="몰디브",
            province=None,
            city=None,
        )
        island = self.place(
            "섬 장소",
            country="몰디브",
            province="Island",
            city=None,
        )
        empty = self.place(
            "연결 없는 장소",
            country="대한민국",
            province="제주",
            city=None,
        )
        same_name = self.place(
            "헐후메일 퍼블릭 비치",
            country="몰디브",
            province="다른 지역",
            city=None,
            latitude=4.3,
            longitude=73.6,
        )
        missing_country = self.place(
            "국가 누락 장소",
            country=None,
            province="Island",
            city=None,
            latitude=4.4,
            longitude=73.7,
        )
        missing_name = MemoryKeeperPlace(
            display_name="   ",
            canonical_name=None,
            country="몰디브",
            province="Island",
            city=None,
            latitude=4.5,
            longitude=73.8,
            active=True,
        )
        self.db.add(missing_name)
        self.db.flush()
        male_file, male_metadata = self.file("h", lat=4.21, lon=73.54)
        lot_file, lot_metadata = self.file("i", lat=4.22, lon=73.55)
        for metadata, city in ((male_metadata, "말레"), (lot_metadata, "Lot")):
            metadata.memorykeeper_place_id = beach.id
            metadata.country = "몰디브"
            metadata.province = None
            metadata.city = city
            metadata.place_name = "헐후메일 퍼블릭 비치"
        for common_file, captured_at in (
            (male_file, datetime(2026, 2, 13, 12, 0)),
            (lot_file, datetime(2026, 2, 14, 12, 0)),
        ):
            self.db.add(
                MemoryKeeperFileState(
                    file_id=common_file.id,
                    effective_capture_datetime=captured_at,
                    effective_capture_date=captured_at.date(),
                    effective_capture_year=2026,
                    effective_capture_precision="DATETIME",
                    date_basis="EXIF",
                )
            )
        self.db.commit()

        response = self.service.hierarchy()
        beach_rows = [
            item
            for item in response.items
            if str(item.memorykeeper_place_id) == beach.id
        ]
        assert {(item.region, item.photo_count) for item in beach_rows} == {
            ("Lot", 1),
            ("말레", 1),
        }
        assert len({item.location_key for item in beach_rows}) == 1
        assert response.total_places == 6
        assert any(
            str(item.memorykeeper_place_id) == empty.id and item.photo_count == 0
            for item in response.items
        )
        assert any(
            str(item.memorykeeper_place_id) == island.id
            and item.region == "Island"
            and item.location_hierarchy_complete
            for item in response.items
        )
        assert {
            str(item.memorykeeper_place_id)
            for item in response.items
            if item.display_name == "헐후메일 퍼블릭 비치"
        } == {beach.id, same_name.id}
        assert next(
            item
            for item in response.items
            if str(item.memorykeeper_place_id) == missing_country.id
        ).location_cleanup_reason == LOCATION_REASON_COUNTRY_MISSING
        assert next(
            item
            for item in response.items
            if str(item.memorykeeper_place_id) == missing_name.id
        ).location_cleanup_reason == LOCATION_REASON_PLACE_MISSING

        gallery_before = MemoryKeeperFastGalleryService(self.db).hierarchy()
        year_before = next(item for item in gallery_before.items if item.year == 2026)
        maldives_before = next(
            item for item in year_before.countries if item.country == "몰디브"
        )
        assert {item.region: item.count for item in maldives_before.regions} == {
            "Lot": 1,
            "말레": 1,
        }

        self.service.update(beach.id, PlaceUpdate(revision=1, city="말레"))
        gallery_after = MemoryKeeperFastGalleryService(self.db).hierarchy()
        year_after = next(item for item in gallery_after.items if item.year == 2026)
        maldives_after = next(
            item for item in year_after.countries if item.country == "몰디브"
        )
        assert [(item.region, item.count) for item in maldives_after.regions] == [
            ("말레", 2)
        ]
        self.db.refresh(male_metadata)
        self.db.refresh(lot_metadata)
        assert male_metadata.city == "말레"
        assert lot_metadata.city == "Lot"
        assert (male_metadata.gps_lat, male_metadata.gps_lon) == (4.21, 73.54)
        assert (lot_metadata.gps_lat, lot_metadata.gps_lon) == (4.22, 73.55)

    def test_gallery_detail_explains_registered_hierarchy_gaps(self) -> None:
        place = self.place(
            "헐후메일 퍼블릭 비치",
            canonical_name="헐후메일 퍼블릭 비치",
            country="몰디브",
            province=None,
            city=None,
            latitude=4.21147,
            longitude=73.54420,
        )
        common_file, metadata = self.file("j", lat=4.21147, lon=73.54420)
        metadata.memorykeeper_place_id = place.id
        metadata.country = "몰디브"
        metadata.province = None
        metadata.city = None
        metadata.district = "Hulhumalé"
        metadata.place_name = None
        self.db.commit()

        detail = GalleryService(self.db).get_detail(
            common_file.file_id,
            service_name="MemoryKeeper",
        )
        assert str(detail.memorykeeper_place_id) == place.id
        assert detail.canonical_country == "몰디브"
        assert detail.canonical_region is None
        assert detail.canonical_place == "헐후메일 퍼블릭 비치"
        assert detail.location_hierarchy_complete is False
        assert detail.location_cleanup_reason == LOCATION_REASON_REGION_MISSING
        assert detail.metadata["district"] == "Hulhumalé"
        assert (detail.metadata["gps_lat"], detail.metadata["gps_lon"]) == (
            4.21147,
            73.54420,
        )

        assert location_hierarchy_reason(
            registered_place_id=place.id,
            country=None,
            region="Island",
            place="장소",
        ) == LOCATION_REASON_COUNTRY_MISSING
        assert location_hierarchy_reason(
            registered_place_id=place.id,
            country="몰디브",
            region="Island",
            place="   ",
        ) == LOCATION_REASON_PLACE_MISSING

    def test_reassign_one_file_moves_gallery_count_and_preserves_raw_location(self) -> None:
        source = self.place(
            "기존 장소",
            country="몰디브",
            city="Lot",
            province=None,
        )
        target = self.place(
            "헐후메일 퍼블릭 비치",
            country="몰디브",
            city="말레",
            province=None,
            latitude=4.21147,
            longitude=73.54420,
        )
        common_file, metadata = self.file("k", lat=4.21147, lon=73.54420)
        metadata.country = "몰디브"
        metadata.province = None
        metadata.city = "Lot"
        metadata.district = "Hulhumalé"
        metadata.place_name = "원본 위치명"
        captured_at = datetime(2026, 2, 14, 12, 0)
        self.db.add(
            MemoryKeeperFileState(
                file_id=common_file.id,
                effective_capture_datetime=captured_at,
                effective_capture_date=captured_at.date(),
                effective_capture_year=2026,
                effective_capture_precision="DATETIME",
                date_basis="EXIF",
            )
        )
        self.db.commit()
        self.service.assign_file(
            public_file_id=common_file.file_id,
            place_id=source.id,
            expected_revision=0,
        )
        raw_before = (
            metadata.gps_lat,
            metadata.gps_lon,
            metadata.country,
            metadata.province,
            metadata.city,
            metadata.district,
            metadata.place_name,
        )

        before = MemoryKeeperFastGalleryService(self.db).hierarchy().items[0]
        assert before.countries[0].regions[0].region == "Lot"
        assert before.countries[0].regions[0].count == 1

        self.service.assign_file(
            public_file_id=common_file.file_id,
            place_id=target.id,
            expected_revision=1,
        )
        after = MemoryKeeperFastGalleryService(self.db).hierarchy().items[0]
        assert after.countries[0].regions[0].region == "말레"
        assert after.countries[0].regions[0].count == 1
        self.db.refresh(metadata)
        assert metadata.memorykeeper_place_id == target.id
        assert metadata.place_match_source == "USER"
        assert raw_before == (
            metadata.gps_lat,
            metadata.gps_lon,
            metadata.country,
            metadata.province,
            metadata.city,
            metadata.district,
            metadata.place_name,
        )

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

    def test_delete_rejects_linked_place_without_changing_relation_or_raw_data(self) -> None:
        place = self.place()
        common_file, metadata = self.file("g")
        self.service.auto_match_file(file_id=common_file.id)
        with self.assertRaises(HTTPException) as raised:
            self.service.delete(place.id, expected_revision=place.revision)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "PLACE_IN_USE")
        self.db.refresh(metadata)
        self.assertEqual(metadata.memorykeeper_place_id, place.id)
        self.assertEqual(metadata.place_name, "원시 역지오코딩 주소")
        self.assertEqual(metadata.gps_lon, 127.5905236)

    def test_delete_soft_deletes_only_unused_place_and_checks_revision(self) -> None:
        place = self.place()
        with self.assertRaises(HTTPException) as conflict:
            self.service.delete(place.id, expected_revision=place.revision + 1)
        self.assertEqual(conflict.exception.status_code, 409)
        deleted = self.service.delete(place.id, expected_revision=place.revision)
        self.assertFalse(deleted.active)
        self.assertIsNotNone(deleted.deleted_at)
        self.assertIsNone(deleted.auto_dedup_key)
        active = self.service.list(
            active=True,
            favorite=None,
            query=None,
            limit=10,
            offset=0,
        )
        self.assertNotIn(place.id, {str(item.id) for item in active.items})

    def test_delete_rejects_non_gallery_fk_reference(self) -> None:
        place = self.place()
        common_file, metadata = self.file("z", services=("AstroJournal",))
        common_file.deleted = True
        metadata.memorykeeper_place_id = place.id
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            self.service.delete(place.id, expected_revision=place.revision)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["reference_count"], 1)
        self.assertEqual(raised.exception.detail["linked_photo_count"], 0)

    def test_duplicate_candidates_are_bounded_and_include_empty_places(self) -> None:
        same_a = self.place(
            "Harbor Lounge",
            country="몰디브",
            city="말레",
            latitude=4.21147,
            longitude=73.54420,
        )
        same_b = self.place(
            "  HARBOR   LOUNGE ",
            country="몰디브",
            city="Lot",
            latitude=4.21150,
            longitude=73.54425,
        )
        similar = self.place(
            "Harbour Lounge",
            country="몰디브",
            city="말레",
            latitude=4.3,
            longitude=73.7,
        )
        far = self.place(
            "Completely Different",
            country="대한민국",
            city="서울",
            latitude=37.5,
            longitude=127.0,
        )
        _common_file, metadata = self.file("u")
        metadata.memorykeeper_place_id = same_a.id
        self.db.commit()

        result = self.service.duplicate_candidates(
            distance_m=100,
            similarity_threshold=0.75,
            scan_limit=500,
            result_limit=100,
        )

        pairs = {
            frozenset((str(item.first.id), str(item.second.id))): item
            for item in result.items
        }
        same_pair = pairs[frozenset((same_a.id, same_b.id))]
        self.assertIn("NORMALIZED_NAME", same_pair.reasons)
        self.assertIn("NEARBY_COORDINATES", same_pair.reasons)
        similar_pair = pairs[frozenset((same_a.id, similar.id))]
        self.assertIn("SIMILAR_NAME_SAME_LOCATION", similar_pair.reasons)
        counts = {
            str(similar_pair.first.id): similar_pair.first.linked_photo_count,
            str(similar_pair.second.id): similar_pair.second.linked_photo_count,
        }
        self.assertEqual(counts[similar.id], 0)
        self.assertFalse(
            any(far.id in pair for pair in pairs)
        )
        self.assertEqual(result.total_active_places, 4)
        self.assertFalse(result.truncated)

    def test_duplicate_candidates_can_return_an_empty_result(self) -> None:
        self.place("서울", latitude=37.5, longitude=127.0)
        self.place("오사카", latitude=34.6, longitude=135.5)
        result = self.service.duplicate_candidates(
            distance_m=25,
            similarity_threshold=0.95,
            scan_limit=500,
            result_limit=100,
        )
        self.assertEqual(result.items, [])
        self.assertFalse(result.truncated)

    def test_linked_photos_are_uuid_exact_counted_and_paginated(self) -> None:
        place = self.place("연결 장소")
        other = self.place("다른 장소", latitude=36.0, longitude=128.0)
        linked = []
        for marker, year in (("a", 2024), ("b", 2025), ("c", 2026)):
            common_file, metadata = self.file(marker)
            metadata.memorykeeper_place_id = place.id
            self.db.add(
                MemoryKeeperFileState(
                    file_id=common_file.id,
                    effective_capture_datetime=datetime(year, 1, 1, 12, 0),
                    effective_capture_date=datetime(year, 1, 1).date(),
                    effective_capture_year=year,
                    effective_capture_precision="DATETIME",
                    date_basis="EXIF",
                )
            )
            linked.append(common_file.file_id)
        unrelated, unrelated_metadata = self.file("d")
        unrelated_metadata.memorykeeper_place_id = other.id
        self.db.commit()

        first = self.service.linked_photos(place.id, limit=2, offset=0)
        second = self.service.linked_photos(place.id, limit=2, offset=2)
        self.assertEqual(first.total, 3)
        self.assertEqual(second.total, 3)
        self.assertEqual(len(first.items), 2)
        self.assertEqual(len(second.items), 1)
        self.assertEqual(
            {item.file_id for item in first.items + second.items},
            set(linked),
        )
        self.assertNotIn(unrelated.file_id, {item.file_id for item in first.items})

    def test_merge_moves_relations_preserves_raw_data_and_updates_gallery(self) -> None:
        source = self.place(
            "중복 장소",
            country="몰디브",
            city="Lot",
            latitude=4.2,
            longitude=73.5,
        )
        target = self.place(
            "기준 장소",
            country="몰디브",
            city="말레",
            latitude=4.21,
            longitude=73.54,
            radius_m=321,
        )
        source_file, source_metadata = self.file("m", lat=4.2, lon=73.5)
        target_file, target_metadata = self.file("n", lat=4.21, lon=73.54)
        source_metadata.memorykeeper_place_id = source.id
        source_metadata.place_match_source = "USER"
        source_metadata.place_match_revision = 4
        original_capture_datetime = datetime(
            2026,
            2,
            14,
            12,
            0,
            tzinfo=timezone.utc,
        )
        source_metadata.datetime_original = original_capture_datetime
        target_metadata.memorykeeper_place_id = target.id
        target_metadata.place_match_source = "RADIUS"
        for common_file in (source_file, target_file):
            self.db.add(
                MemoryKeeperFileState(
                    file_id=common_file.id,
                    effective_capture_datetime=datetime(2026, 2, 14, 12, 0),
                    effective_capture_date=datetime(2026, 2, 14).date(),
                    effective_capture_year=2026,
                    effective_capture_precision="DATETIME",
                    date_basis="EXIF",
                )
            )
        self.db.commit()
        self.db.refresh(source_metadata)
        self.assertEqual(
            source_metadata.datetime_original.replace(tzinfo=timezone.utc),
            original_capture_datetime,
        )
        raw_before = (
            source_metadata.gps_lat,
            source_metadata.gps_lon,
            source_metadata.country,
            source_metadata.province,
            source_metadata.city,
            source_metadata.district,
            source_metadata.place_name,
            source_metadata.datetime_original,
        )

        preview = self.service.merge_preview(
            source_place_id=source.id,
            target_place_id=target.id,
        )
        self.assertTrue(preview.can_merge)
        self.assertEqual(preview.source.linked_photo_count, 1)
        self.assertEqual(preview.target.linked_photo_count, 1)
        self.assertEqual(preview.merged_linked_photo_count, 2)
        result = self.service.merge(
            PlaceMergeRequest(
                source_place_id=source.id,
                target_place_id=target.id,
                source_revision=source.revision,
                target_revision=target.revision,
            )
        )

        self.db.refresh(source)
        self.db.refresh(source_metadata)
        self.assertFalse(source.active)
        self.assertIsNotNone(source.deleted_at)
        self.assertEqual(source_metadata.memorykeeper_place_id, target.id)
        self.assertEqual(source_metadata.place_match_source, "USER")
        self.assertEqual(
            raw_before,
            (
                source_metadata.gps_lat,
                source_metadata.gps_lon,
                source_metadata.country,
                source_metadata.province,
                source_metadata.city,
                source_metadata.district,
                source_metadata.place_name,
                source_metadata.datetime_original,
            ),
        )
        self.assertEqual(result.moved_reference_count, 1)
        self.assertEqual(result.target.linked_photo_count, 2)
        self.assertEqual(result.target.display_name, "기준 장소")
        self.assertEqual(result.target.radius_m, 321)
        leaves = [
            leaf
            for year in MemoryKeeperFastGalleryService(self.db).hierarchy().items
            for country in year.countries
            for region in country.regions
            for leaf in region.places
        ]
        self.assertEqual(
            sum(leaf.count for leaf in leaves if leaf.memorykeeper_place_id == target.id),
            2,
        )
        self.assertFalse(
            any(leaf.memorykeeper_place_id == source.id for leaf in leaves)
        )

    def test_merge_rejects_invalid_state_and_rolls_back_atomically(self) -> None:
        source = self.place("Source")
        target = self.place("Target", latitude=36.0, longitude=128.0)
        first, first_metadata = self.file("x")
        second, second_metadata = self.file("y")
        first_metadata.memorykeeper_place_id = source.id
        second_metadata.memorykeeper_place_id = source.id
        self.db.commit()

        with self.assertRaises(HTTPException) as same:
            self.service.merge(
                PlaceMergeRequest(
                    source_place_id=source.id,
                    target_place_id=source.id,
                    source_revision=source.revision,
                    target_revision=source.revision,
                )
            )
        self.assertEqual(same.exception.status_code, 422)
        with self.assertRaises(HTTPException) as conflict:
            self.service.merge(
                PlaceMergeRequest(
                    source_place_id=source.id,
                    target_place_id=target.id,
                    source_revision=source.revision + 1,
                    target_revision=target.revision,
                )
            )
        self.assertEqual(conflict.exception.status_code, 409)

        original = self.service._set_relation
        calls = 0

        def fail_second_relation(**kwargs):
            nonlocal calls
            calls += 1
            original(**kwargs)
            if calls == 2:
                raise RuntimeError("injected merge failure")

        with patch.object(self.service, "_set_relation", side_effect=fail_second_relation):
            with self.assertRaisesRegex(RuntimeError, "injected merge failure"):
                self.service.merge(
                    PlaceMergeRequest(
                        source_place_id=source.id,
                        target_place_id=target.id,
                        source_revision=source.revision,
                        target_revision=target.revision,
                    )
                )
        self.db.refresh(first_metadata)
        self.db.refresh(second_metadata)
        self.db.refresh(source)
        self.assertEqual(first_metadata.memorykeeper_place_id, source.id)
        self.assertEqual(second_metadata.memorykeeper_place_id, source.id)
        self.assertTrue(source.active)
        self.assertIsNone(source.deleted_at)

    def test_merge_supports_empty_source_and_rejects_replay_or_inactive_target(self) -> None:
        source = self.place("빈 중복 장소")
        target = self.place("기준 장소", latitude=36.0, longitude=128.0)
        result = self.service.merge(
            PlaceMergeRequest(
                source_place_id=source.id,
                target_place_id=target.id,
                source_revision=source.revision,
                target_revision=target.revision,
            )
        )
        self.assertEqual(result.moved_reference_count, 0)
        self.assertEqual(result.moved_linked_photo_count, 0)
        with self.assertRaises(HTTPException) as replay:
            self.service.merge(
                PlaceMergeRequest(
                    source_place_id=source.id,
                    target_place_id=target.id,
                    source_revision=source.revision,
                    target_revision=result.target.revision,
                )
            )
        self.assertEqual(replay.exception.status_code, 409)
        self.assertIn("SOURCE_DELETED", replay.exception.detail["reasons"])

        another = self.place("다른 장소", latitude=37.0, longitude=129.0)
        target.active = False
        self.db.commit()
        preview = self.service.merge_preview(
            source_place_id=another.id,
            target_place_id=target.id,
        )
        self.assertFalse(preview.can_merge)
        self.assertIn("TARGET_INACTIVE", preview.blocking_reasons)

    def test_reclassify_preserves_user_decisions_and_updates_automatic_relations(self) -> None:
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
        self.assertEqual(result.reassigned, 0)
        self.db.refresh(assigned_metadata)
        self.assertEqual(assigned_metadata.memorykeeper_place_id, other.id)

        manual_target, manual_target_metadata = self.file("m")
        self.service.assign_file(
            public_file_id=manual_target.file_id,
            place_id=target.id,
            expected_revision=0,
        )
        target.latitude = 36
        target.longitude = 128
        self.db.commit()
        result = self.service.reclassify(target.id, reassign_from_other_places=False)
        self.assertEqual(result.unassigned_outside_radius, 1)
        self.db.refresh(manual_target_metadata)
        self.assertEqual(manual_target_metadata.memorykeeper_place_id, target.id)

    def test_reclassify_preserves_user_null_and_daily(self) -> None:
        target = self.place(radius_m=500)
        user_null, user_null_metadata = self.file("n")
        daily, daily_metadata = self.file("o")
        self.service.assign_file(
            public_file_id=user_null.file_id,
            place_id=None,
            expected_revision=0,
        )
        self.db.add(
            MemoryKeeperFileState(
                file_id=daily.id,
                photo_category="DAILY",
            )
        )
        # Isolate the category guard from the independent USER-source guard.
        daily_metadata.place_match_source = "AUTO_PLACE_MATCH"
        self.db.commit()

        self.assertFalse(self.service.auto_match_file(file_id=user_null.id))
        self.assertFalse(self.service.auto_match_file(file_id=daily.id))
        result = self.service.reclassify(
            target.id,
            reassign_from_other_places=True,
        )
        self.assertEqual(result.assigned, 0)
        self.assertEqual(result.reassigned, 0)
        self.db.refresh(user_null_metadata)
        self.db.refresh(daily_metadata)
        self.assertIsNone(user_null_metadata.memorykeeper_place_id)
        self.assertIsNone(daily_metadata.memorykeeper_place_id)
        backfill = backfill_memorykeeper_places(self.db, execute=False)
        self.assertEqual(backfill.scanned, 2)
        self.assertEqual(backfill.unchanged, 2)

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
            "/api/memorykeeper/places/duplicate-candidates",
            "/api/memorykeeper/places/merge-preview",
            "/api/memorykeeper/places/merge",
            "/api/memorykeeper/places/match",
            "/api/memorykeeper/places/radius-impact",
            "/api/memorykeeper/places/{place_id}/photos",
            "/api/memorykeeper/places/{place_id}/reclassify",
            "/api/memorykeeper/files/{file_id}/place",
        ):
            self.assertIn(path, paths)
        self.assertTrue(paths["/api/memorykeeper/places"]["get"]["security"])


if __name__ == "__main__":
    unittest.main()

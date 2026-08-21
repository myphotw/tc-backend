from __future__ import annotations

from datetime import datetime, timezone
import re
import unicodedata

from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.repositories.change_event_repository import ChangeEventRepository, ChangeOperation
from app.common.repositories.history_repository import HistoryRepository
from app.common.repositories.metadata_priority import MetadataPriority
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.repositories.place_repository import MemoryKeeperPlaceRepository
from app.memorykeeper.schemas.place import (
    FilePlaceResponse,
    PlaceCreate,
    PlaceListResponse,
    PlaceMatchRequest,
    PlaceMatchResponse,
    PlaceOverlap,
    PlaceResponse,
    PlaceUpdate,
    RadiusImpactRequest,
    RadiusImpactResponse,
    ReclassifyResponse,
)
from app.memorykeeper.services.place_matcher import MemoryKeeperPlaceMatcher, PlaceMatchSource
from app.memorykeeper.services.place_candidate_service import (
    AutoPlaceCandidate,
    MemoryKeeperPlaceCandidateService,
)


class MemoryKeeperPlaceService:
    SERVICE_NAME = "MemoryKeeper"
    PLACE_RESOURCE = "MemoryKeeperPlace"
    FILE_PLACE_RESOURCE = "MemoryKeeperFilePlace"

    def __init__(
        self,
        db: Session,
        *,
        candidate_service: MemoryKeeperPlaceCandidateService | None = None,
    ) -> None:
        self.db = db
        self.repository = MemoryKeeperPlaceRepository(db)
        self.matcher = MemoryKeeperPlaceMatcher(db)
        self.candidates = candidate_service or MemoryKeeperPlaceCandidateService(db)
        self.history = HistoryRepository(db)
        self.changes = ChangeEventRepository(db)

    def create(self, payload: PlaceCreate) -> MemoryKeeperPlace:
        place = MemoryKeeperPlace(**payload.model_dump())
        self.repository.create(place)
        self._append_place_change(place, ChangeOperation.CREATE)
        self.db.commit()
        self.db.refresh(place)
        return place

    def get(self, place_id: str) -> MemoryKeeperPlace:
        place = self.repository.get(place_id)
        if place is None:
            raise HTTPException(status_code=404, detail="Place not found")
        return place

    def list(self, **kwargs) -> PlaceListResponse:
        items, total = self.repository.list(**kwargs)
        return PlaceListResponse(items=items, total=total, limit=kwargs["limit"], offset=kwargs["offset"])

    def update(self, place_id: str, payload: PlaceUpdate) -> MemoryKeeperPlace:
        current = self.get(place_id)
        if current.revision != payload.revision:
            self._revision_conflict(current, payload.revision)
        values = payload.model_dump(exclude={"revision"}, exclude_unset=True)
        updated = self.repository.update_if_revision(place_id, revision=payload.revision, values=values)
        if updated is None:
            latest = self.get(place_id)
            self._revision_conflict(latest, payload.revision)
        self._append_place_change(updated, ChangeOperation.UPDATE)
        self.db.commit()
        self.db.refresh(updated)
        return updated

    def delete(self, place_id: str) -> MemoryKeeperPlace:
        place = self.get(place_id)
        now = datetime.now(timezone.utc)
        metadata_rows = (
            self.db.query(CommonFileMetadata)
            .filter(CommonFileMetadata.memorykeeper_place_id == place.id)
            .all()
        )
        for metadata in metadata_rows:
            common_file = self.db.get(CommonFile, metadata.file_id)
            self._set_relation(
                metadata=metadata,
                common_file=common_file,
                place=None,
                source=PlaceMatchSource.PLACE_DELETED,
                distance_m=None,
                touch_usage=False,
            )
        place.active = False
        place.deleted_at = now
        place.revision = int(place.revision) + 1
        place.updated_at = now
        self._append_place_change(place, ChangeOperation.DELETE, tombstone=True)
        self.db.commit()
        self.db.refresh(place)
        return place

    def match(self, payload: PlaceMatchRequest) -> PlaceMatchResponse:
        match = self.matcher.match(
            gps_lat=payload.latitude,
            gps_lon=payload.longitude,
            provider_place_id=payload.provider_place_id,
            canonical_name=payload.canonical_name,
        )
        return PlaceMatchResponse(
            matched=match.matched,
            place=match.place,
            distance_m=match.distance_m,
            match_source=match.source,
        )

    def assign_file(
        self,
        *,
        public_file_id: str,
        place_id: str | None,
        expected_revision: int,
    ) -> FilePlaceResponse:
        common_file = (
            self.db.query(CommonFile)
            .filter(CommonFile.file_id == public_file_id)
            .filter(CommonFile.deleted.is_(False))
            .first()
        )
        if common_file is None or not self.repository.has_memorykeeper_link(common_file.id):
            raise HTTPException(status_code=404, detail="MemoryKeeper file not found")
        metadata = (
            self.db.query(CommonFileMetadata)
            .filter(CommonFileMetadata.file_id == common_file.id)
            .first()
        )
        if metadata is None:
            metadata = CommonFileMetadata(file_id=common_file.id)
            self.db.add(metadata)
            self.db.flush()
        current_revision = int(metadata.place_match_revision or 0)
        if current_revision != expected_revision:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "REVISION_CONFLICT", "expected_revision": expected_revision, "current_revision": current_revision},
            )
        place = self.get(place_id) if place_id is not None else None
        distance = self._distance(metadata, place)
        self._set_relation(metadata=metadata, common_file=common_file, place=place, source=PlaceMatchSource.USER, distance_m=distance)
        self.db.commit()
        self.db.refresh(metadata)
        return self.file_place_response(common_file, metadata)

    def auto_match_file(
        self,
        *,
        file_id: int,
        create_missing: bool = True,
    ) -> bool:
        if not self.repository.has_memorykeeper_link(file_id):
            return False
        metadata = self.db.query(CommonFileMetadata).filter(CommonFileMetadata.file_id == file_id).first()
        if metadata is None or metadata.gps_lat is None or metadata.gps_lon is None:
            return False
        # A user's explicit assignment or explicit unlink is authoritative.
        if metadata.place_match_source == PlaceMatchSource.USER:
            return False
        match = self.matcher.match(
            gps_lat=float(metadata.gps_lat),
            gps_lon=float(metadata.gps_lon),
            canonical_name=metadata.place_name,
        )
        if not match.matched:
            if create_missing:
                candidate = self.build_auto_candidate(metadata)
                place, created, source = self._resolve_or_create_candidate(
                    candidate,
                    photo_lat=float(metadata.gps_lat),
                    photo_lon=float(metadata.gps_lon),
                )
                # A unique-key race rolls the transaction back, so reload rows.
                metadata = (
                    self.db.query(CommonFileMetadata)
                    .filter(CommonFileMetadata.file_id == file_id)
                    .first()
                )
                common_file = self.db.get(CommonFile, file_id)
                self._set_relation(
                    metadata=metadata,
                    common_file=common_file,
                    place=place,
                    source=(PlaceMatchSource.AUTO_CREATED if created else source),
                    distance_m=self._distance(metadata, place),
                )
                self.db.commit()
                return True
            if metadata.memorykeeper_place_id is not None:
                common_file = self.db.get(CommonFile, file_id)
                self._set_relation(
                    metadata=metadata,
                    common_file=common_file,
                    place=None,
                    source=PlaceMatchSource.AUTO_PLACE_MATCH,
                    distance_m=None,
                    touch_usage=False,
                )
                self.db.commit()
            return False
        common_file = self.db.get(CommonFile, file_id)
        self._set_relation(metadata=metadata, common_file=common_file, place=match.place, source=match.source, distance_m=match.distance_m)
        self.db.commit()
        return True

    def build_auto_candidate(
        self,
        metadata: CommonFileMetadata,
    ) -> AutoPlaceCandidate:
        return self.candidates.choose(metadata)

    def preview_auto_candidate(
        self,
        metadata: CommonFileMetadata,
    ) -> tuple[AutoPlaceCandidate, MemoryKeeperPlace | None]:
        candidate = self.build_auto_candidate(metadata)
        duplicate, _ = self._candidate_duplicate(
            candidate,
            photo_lat=float(metadata.gps_lat),
            photo_lon=float(metadata.gps_lon),
        )
        return candidate, duplicate

    def reclassify(self, place_id: str, *, reassign_from_other_places: bool) -> ReclassifyResponse:
        place = self.get(place_id)
        if not place.active:
            raise HTTPException(status_code=422, detail="Inactive place cannot be reclassified")
        scanned = assigned = reassigned = outside = unchanged = 0
        for common_file, metadata in self.repository.memorykeeper_files_with_gps():
            scanned += 1
            distance = self.matcher.distance_m(float(metadata.gps_lat), float(metadata.gps_lon), place.latitude, place.longitude)
            current = metadata.memorykeeper_place_id
            if current == place.id and distance > place.radius_m:
                self._set_relation(metadata=metadata, common_file=common_file, place=None, source=PlaceMatchSource.AUTO_PLACE_MATCH, distance_m=None, touch_usage=False)
                outside += 1
            elif distance <= place.radius_m and current is None:
                self._set_relation(metadata=metadata, common_file=common_file, place=place, source=PlaceMatchSource.RADIUS, distance_m=distance)
                assigned += 1
            elif distance <= place.radius_m and current != place.id and reassign_from_other_places:
                self._set_relation(metadata=metadata, common_file=common_file, place=place, source=PlaceMatchSource.RADIUS, distance_m=distance)
                reassigned += 1
            else:
                unchanged += 1
        self.db.commit()
        return ReclassifyResponse(
            place_id=place.id,
            scanned=scanned,
            assigned=assigned,
            reassigned=reassigned,
            unassigned_outside_radius=outside,
            unchanged=unchanged,
        )

    def radius_impact(self, payload: RadiusImpactRequest) -> RadiusImpactResponse:
        affected: list[str] = []
        for common_file, metadata in self.repository.memorykeeper_files_with_gps():
            distance = self.matcher.distance_m(payload.latitude, payload.longitude, float(metadata.gps_lat), float(metadata.gps_lon))
            if distance <= payload.radius_m:
                affected.append(common_file.file_id)
        overlaps: list[PlaceOverlap] = []
        own_id = str(payload.place_id) if payload.place_id is not None else None
        for place in self.repository.active_places():
            if place.id == own_id:
                continue
            distance = self.matcher.distance_m(payload.latitude, payload.longitude, place.latitude, place.longitude)
            if distance <= payload.radius_m + place.radius_m:
                overlaps.append(PlaceOverlap(place=place, center_distance_m=distance))
        overlaps.sort(key=lambda item: (item.center_distance_m, str(item.place.id)))
        return RadiusImpactResponse(matched_file_count=len(affected), affected_file_ids=affected, overlapping_places=overlaps)

    def file_place_response(self, common_file: CommonFile, metadata: CommonFileMetadata) -> FilePlaceResponse:
        place = self.repository.get(metadata.memorykeeper_place_id) if metadata.memorykeeper_place_id else None
        return FilePlaceResponse(
            file_id=common_file.file_id,
            memorykeeper_place_id=metadata.memorykeeper_place_id,
            place_display_name=(
                place.display_name
                if place
                else (metadata.place_name or "미분류")
            ),
            place_canonical_name=place.canonical_name if place else None,
            geocoded_place_name=metadata.place_name,
            place_match_source=metadata.place_match_source,
            place_match_distance_m=metadata.place_match_distance_m,
            place_revision=int(metadata.place_match_revision or 0),
        )

    def _set_relation(
        self,
        *,
        metadata: CommonFileMetadata,
        common_file: CommonFile | None,
        place: MemoryKeeperPlace | None,
        source: str,
        distance_m: float | None,
        touch_usage: bool = True,
    ) -> None:
        old_values = {
            "memorykeeper_place_id": metadata.memorykeeper_place_id,
            "place_match_source": metadata.place_match_source,
            "place_match_distance_m": metadata.place_match_distance_m,
            "place_match_revision": int(metadata.place_match_revision or 0),
        }
        new_revision = old_values["place_match_revision"] + 1
        new_values = {
            "memorykeeper_place_id": place.id if place else None,
            "place_match_source": source,
            "place_match_distance_m": distance_m,
            "place_match_revision": new_revision,
        }
        if all(old_values[name] == new_values[name] for name in new_values if name != "place_match_revision"):
            return
        for name, value in new_values.items():
            setattr(metadata, name, value)
        self.history.create_histories(
            items=[
                {"file_id": metadata.file_id, "field_name": name, "old_value": old_values[name], "new_value": value, "source": source, "priority": MetadataPriority.USER if source == PlaceMatchSource.USER else MetadataPriority.SYSTEM, "modified_by": "MemoryKeeperPlaceService", "approved": source == PlaceMatchSource.USER}
                for name, value in new_values.items()
                if old_values[name] != value
            ],
            commit=False,
        )
        if place is not None and touch_usage:
            self.repository.touch_usage(place)
        if common_file is not None:
            self.changes.append(service_name=self.SERVICE_NAME, resource_type=self.FILE_PLACE_RESOURCE, resource_id=common_file.file_id, operation=ChangeOperation.UPDATE, revision=new_revision)

    def _distance(self, metadata: CommonFileMetadata, place: MemoryKeeperPlace | None) -> float | None:
        if place is None or metadata.gps_lat is None or metadata.gps_lon is None:
            return None
        return self.matcher.distance_m(float(metadata.gps_lat), float(metadata.gps_lon), place.latitude, place.longitude)

    def _resolve_or_create_candidate(
        self,
        candidate: AutoPlaceCandidate,
        *,
        photo_lat: float,
        photo_lon: float,
    ) -> tuple[MemoryKeeperPlace, bool, str]:
        duplicate, source = self._candidate_duplicate(
            candidate,
            photo_lat=photo_lat,
            photo_lon=photo_lon,
        )
        if duplicate is not None:
            return duplicate, False, source

        dedup_key = self._auto_dedup_key(candidate)
        place = MemoryKeeperPlace(
            display_name=candidate.display_name,
            canonical_name=candidate.canonical_name,
            address=candidate.address,
            country=candidate.country,
            province=candidate.province,
            city=candidate.city,
            district=candidate.district,
            latitude=candidate.latitude,
            longitude=candidate.longitude,
            radius_m=candidate.radius_m,
            provider_place_id=candidate.provider_place_id,
            category=candidate.category,
            active=True,
            favorite=False,
            usage_count=0,
            creation_source=candidate.creation_source,
            auto_dedup_key=dedup_key,
        )
        try:
            self.repository.create(place)
            self._append_place_change(place, ChangeOperation.CREATE)
            self.db.flush()
            return place, True, PlaceMatchSource.AUTO_CREATED
        except IntegrityError:
            self.db.rollback()
            existing = self.repository.get_by_auto_dedup_key(dedup_key)
            if existing is None:
                raise
            return existing, False, self._duplicate_source(existing, candidate)

    def _candidate_duplicate(
        self,
        candidate: AutoPlaceCandidate,
        *,
        photo_lat: float,
        photo_lon: float,
    ) -> tuple[MemoryKeeperPlace | None, str]:
        exact = self.matcher.match(
            gps_lat=photo_lat,
            gps_lon=photo_lon,
            provider_place_id=candidate.provider_place_id,
            canonical_name=candidate.canonical_name,
        )
        if exact.place is not None:
            return exact.place, exact.source
        normalized_display = self._normalized_name(candidate.display_name)
        for place in self.repository.active_places():
            if self._normalized_name(place.display_name) != normalized_display:
                continue
            distance = self.matcher.distance_m(
                candidate.latitude,
                candidate.longitude,
                place.latitude,
                place.longitude,
            )
            if distance <= max(float(place.radius_m), candidate.radius_m):
                return place, PlaceMatchSource.RADIUS
        return None, PlaceMatchSource.NONE

    @classmethod
    def _auto_dedup_key(cls, candidate: AutoPlaceCandidate) -> str:
        if candidate.provider_place_id:
            return f"provider:{candidate.provider_place_id.strip()}"
        return (
            f"name:{cls._normalized_name(candidate.canonical_name)}:"
            f"{candidate.latitude:.3f}:{candidate.longitude:.3f}"
        )

    @staticmethod
    def _normalized_name(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold().strip()
        return re.sub(r"\s+", " ", normalized)

    @staticmethod
    def _duplicate_source(
        place: MemoryKeeperPlace,
        candidate: AutoPlaceCandidate,
    ) -> str:
        if candidate.provider_place_id and place.provider_place_id == candidate.provider_place_id:
            return PlaceMatchSource.PROVIDER_PLACE_ID
        if (
            place.canonical_name
            and place.canonical_name.strip().casefold()
            == candidate.canonical_name.strip().casefold()
        ):
            return PlaceMatchSource.CANONICAL_NAME
        return PlaceMatchSource.RADIUS

    def _append_place_change(self, place: MemoryKeeperPlace, operation: str, *, tombstone: bool = False) -> None:
        self.changes.append(service_name=self.SERVICE_NAME, resource_type=self.PLACE_RESOURCE, resource_id=place.id, operation=operation, revision=place.revision, tombstone=tombstone)

    @staticmethod
    def _revision_conflict(place: MemoryKeeperPlace, expected: int) -> None:
        raise HTTPException(status_code=409, detail={"code": "REVISION_CONFLICT", "place_id": place.id, "expected_revision": expected, "current_revision": place.revision})

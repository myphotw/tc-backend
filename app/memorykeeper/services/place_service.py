from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
import re
import unicodedata

from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.repositories.change_event_repository import ChangeEventRepository, ChangeOperation
from app.common.repositories.file_service_repository import FileServiceRepository
from app.common.repositories.history_repository import HistoryRepository
from app.common.repositories.metadata_priority import MetadataPriority
from app.common.services.gallery_media import build_gallery_media_url
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.repositories.place_repository import MemoryKeeperPlaceRepository
from app.memorykeeper.schemas.place import (
    FilePlaceResponse,
    PlaceDuplicateCandidate,
    PlaceDuplicateCandidatesResponse,
    PlaceCreate,
    PlaceHierarchyItem,
    PlaceHierarchyResponse,
    PlaceLinkedPhotoItem,
    PlaceLinkedPhotosResponse,
    PlaceListResponse,
    PlaceManagementItem,
    PlaceMatchRequest,
    PlaceMatchResponse,
    PlaceMergePreviewResponse,
    PlaceMergeRequest,
    PlaceMergeResponse,
    PlaceOverlap,
    PlaceResponse,
    PlaceUpdate,
    RadiusImpactRequest,
    RadiusImpactResponse,
    ReclassifyResponse,
)
from app.memorykeeper.services.place_matcher import MemoryKeeperPlaceMatcher, PlaceMatchSource
from app.memorykeeper.services.fast_gallery_location import encode_registered_location_key
from app.memorykeeper.repositories.place_cleanup_repository import location_hierarchy_reason
from app.memorykeeper.services.capture_date_service import MemoryKeeperCaptureDateService
from app.memorykeeper.services.photo_classification_policy import (
    MemoryKeeperPhotoCategory,
    blocks_automatic_place_change,
)
from app.memorykeeper.services.place_candidate_service import (
    AutoPlaceCandidate,
    MemoryKeeperPlaceCandidateService,
)


class MemoryKeeperPlaceService:
    SERVICE_NAME = "MemoryKeeper"
    PLACE_RESOURCE = "MemoryKeeperPlace"
    FILE_PLACE_RESOURCE = "MemoryKeeperFilePlace"
    PHOTO_CATEGORY_RESOURCE = "MemoryKeeperPhotoCategory"

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

    def get_assignable(self, place_id: str) -> MemoryKeeperPlace:
        """Lock and validate a Place before locking files that will reference it."""
        place = self.repository.get(place_id, lock=True)
        if place is None:
            raise HTTPException(status_code=404, detail="Place not found")
        if not place.active:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Inactive place cannot be assigned",
            )
        return place

    def list(self, **kwargs) -> PlaceListResponse:
        items, total = self.repository.list(**kwargs)
        return PlaceListResponse(items=items, total=total, limit=kwargs["limit"], offset=kwargs["offset"])

    def hierarchy(self) -> PlaceHierarchyResponse:
        items: list[PlaceHierarchyItem] = []
        place_ids: set[str] = set()
        for row in self.repository.hierarchy_rows():
            place_id = str(row.memorykeeper_place_id)
            reason = location_hierarchy_reason(
                registered_place_id=place_id,
                country=row.country,
                region=row.region,
                place=row.display_name,
            )
            path = [
                str(value).strip()
                for value in (row.country, row.region, row.display_name)
                if value is not None and str(value).strip()
            ]
            items.append(
                PlaceHierarchyItem(
                    memorykeeper_place_id=place_id,
                    location_key=encode_registered_location_key(place_id),
                    country=row.country,
                    region=row.region,
                    display_name=row.display_name,
                    location_path=path,
                    full_path=" > ".join(path),
                    photo_count=int(row.photo_count or 0),
                    location_hierarchy_complete=reason is None,
                    location_cleanup_reason=reason,
                )
            )
            place_ids.add(place_id)
        return PlaceHierarchyResponse(
            items=items,
            total_places=len(place_ids),
            total_paths=len(items),
        )

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

    def delete(
        self,
        place_id: str,
        *,
        expected_revision: int | None = None,
    ) -> MemoryKeeperPlace:
        """Soft-delete only an unused Place; never silently orphan photos."""
        try:
            place = self.repository.get(place_id, include_deleted=True, lock=True)
            if place is None or place.deleted_at is not None:
                raise HTTPException(status_code=404, detail="Place not found")
            if expected_revision is not None and place.revision != expected_revision:
                self._revision_conflict(place, expected_revision)
            reference_count = self.repository.total_reference_count(place.id)
            if reference_count:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "PLACE_IN_USE",
                        "place_id": place.id,
                        "reference_count": reference_count,
                        "linked_photo_count": self.repository.linked_photo_count(
                            place.id
                        ),
                        "message": "Merge this Place before deleting it",
                    },
                )
            now = datetime.now(timezone.utc)
            place.active = False
            place.deleted_at = now
            place.auto_dedup_key = None
            place.revision = int(place.revision) + 1
            place.updated_at = now
            self._append_place_change(place, ChangeOperation.DELETE, tombstone=True)
            self.db.commit()
            self.db.refresh(place)
            return place
        except Exception:
            self.db.rollback()
            raise

    def linked_photos(
        self,
        place_id: str,
        *,
        limit: int,
        offset: int,
    ) -> PlaceLinkedPhotosResponse:
        self.get(place_id)
        rows, total = self.repository.linked_photos(
            place_id,
            limit=limit,
            offset=offset,
        )
        return PlaceLinkedPhotosResponse(
            items=[
                PlaceLinkedPhotoItem(
                    common_file_id=common_file.id,
                    file_id=common_file.file_id,
                    filename=common_file.original_name,
                    thumbnail_url=build_gallery_media_url(
                        common_file.file_id,
                        "thumbnail",
                        common_file.thumb_path,
                    ),
                    effective_capture_datetime=(
                        state.effective_capture_datetime if state else None
                    ),
                    effective_capture_date=(
                        state.effective_capture_date if state else None
                    ),
                    effective_capture_year=(
                        state.effective_capture_year if state else None
                    ),
                    place_revision=int(metadata.place_match_revision or 0),
                )
                for common_file, metadata, state in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    def duplicate_candidates(
        self,
        *,
        distance_m: float,
        similarity_threshold: float,
        scan_limit: int,
        result_limit: int,
    ) -> PlaceDuplicateCandidatesResponse:
        places, total = self.repository.active_places_bounded(limit=scan_limit)
        counts = self.repository.linked_photo_counts([place.id for place in places])
        items: list[PlaceDuplicateCandidate] = []
        for index, first in enumerate(places):
            first_name = self._normalized_name(first.display_name)
            first_country = self._normalized_optional(first.country)
            first_region = self._normalized_optional(first.city or first.province)
            for second in places[index + 1 :]:
                second_name = self._normalized_name(second.display_name)
                similarity = SequenceMatcher(None, first_name, second_name).ratio()
                center_distance = self.matcher.distance_m(
                    first.latitude,
                    first.longitude,
                    second.latitude,
                    second.longitude,
                )
                reasons = []
                if first_name and first_name == second_name:
                    reasons.append("NORMALIZED_NAME")
                elif (
                    first_country
                    and first_country == self._normalized_optional(second.country)
                    and first_region
                    and first_region
                    == self._normalized_optional(second.city or second.province)
                    and similarity >= similarity_threshold
                ):
                    reasons.append("SIMILAR_NAME_SAME_LOCATION")
                if center_distance <= distance_m:
                    reasons.append("NEARBY_COORDINATES")
                if not reasons:
                    continue
                items.append(
                    PlaceDuplicateCandidate(
                        first=self._management_item(first, counts.get(first.id, 0)),
                        second=self._management_item(second, counts.get(second.id, 0)),
                        reasons=reasons,
                        name_similarity=round(similarity, 4),
                        center_distance_m=round(center_distance, 3),
                    )
                )
        items.sort(
            key=lambda item: (
                -len(item.reasons),
                item.center_distance_m,
                -item.name_similarity,
                str(item.first.id),
                str(item.second.id),
            )
        )
        return PlaceDuplicateCandidatesResponse(
            items=items[:result_limit],
            scanned_places=len(places),
            total_active_places=total,
            truncated=total > len(places) or len(items) > result_limit,
            distance_m=distance_m,
            similarity_threshold=similarity_threshold,
        )

    def merge_preview(
        self,
        *,
        source_place_id: str,
        target_place_id: str,
    ) -> PlaceMergePreviewResponse:
        source = self.repository.get(source_place_id, include_deleted=True)
        target = self.repository.get(target_place_id, include_deleted=True)
        if source is None or target is None:
            raise HTTPException(status_code=404, detail="Place not found")
        counts = self.repository.linked_photo_counts(
            list({source_place_id, target_place_id})
        )
        source_count = counts.get(source.id, 0)
        target_count = counts.get(target.id, 0)
        reasons = self._merge_blocking_reasons(source, target)
        return PlaceMergePreviewResponse(
            source=self._management_item(source, source_count),
            target=self._management_item(target, target_count),
            merged_linked_photo_count=(
                source_count if source.id == target.id else source_count + target_count
            ),
            source_total_reference_count=self.repository.total_reference_count(
                source.id
            ),
            target_total_reference_count=self.repository.total_reference_count(
                target.id
            ),
            other_reference_counts={},
            can_merge=not reasons,
            blocking_reasons=reasons,
        )

    def merge(self, payload: PlaceMergeRequest) -> PlaceMergeResponse:
        source_id = str(payload.source_place_id)
        target_id = str(payload.target_place_id)
        if source_id == target_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "SAME_PLACE", "message": "Source and target must differ"},
            )
        try:
            locked = self.repository.get_many_for_update([source_id, target_id])
            by_id = {place.id: place for place in locked}
            source = by_id.get(source_id)
            target = by_id.get(target_id)
            if source is None or target is None:
                raise HTTPException(status_code=404, detail="Place not found")
            reasons = self._merge_blocking_reasons(source, target)
            if reasons:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "PLACE_MERGE_BLOCKED", "reasons": reasons},
                )
            if source.revision != payload.source_revision:
                self._revision_conflict(source, payload.source_revision)
            if target.revision != payload.target_revision:
                self._revision_conflict(target, payload.target_revision)

            relation_rows = self.repository.relation_rows_for_update(
                [source.id, target.id]
            )
            linked_before = self.repository.linked_photo_count(source.id)
            source_rows = [
                (metadata, common_file)
                for metadata, common_file in relation_rows
                if metadata.memorykeeper_place_id == source.id
            ]
            for metadata, common_file in source_rows:
                self._set_relation(
                    metadata=metadata,
                    common_file=common_file,
                    place=target,
                    source=(metadata.place_match_source or PlaceMatchSource.USER),
                    distance_m=self._distance(metadata, target),
                    touch_usage=False,
                )

            now = datetime.now(timezone.utc)
            source.active = False
            source.deleted_at = now
            source.auto_dedup_key = None
            source.revision = int(source.revision) + 1
            source.updated_at = now
            target.revision = int(target.revision) + 1
            target.updated_at = now
            self._append_place_change(source, ChangeOperation.DELETE, tombstone=True)
            self._append_place_change(target, ChangeOperation.UPDATE)
            self.db.commit()
            self.db.refresh(target)
            return PlaceMergeResponse(
                target=self._management_item(
                    target,
                    self.repository.linked_photo_count(target.id),
                ),
                source_place_id=source.id,
                moved_reference_count=len(source_rows),
                moved_linked_photo_count=linked_before,
            )
        except Exception:
            self.db.rollback()
            raise

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
        place = self.get_assignable(place_id) if place_id is not None else None
        common_file = (
            self.db.query(CommonFile)
            .filter(CommonFile.file_id == public_file_id)
            .filter(CommonFile.deleted.is_(False))
            .with_for_update()
            .first()
        )
        if common_file is None or not self.repository.has_memorykeeper_link(common_file.id):
            raise HTTPException(status_code=404, detail="MemoryKeeper file not found")
        metadata = (
            self.db.query(CommonFileMetadata)
            .filter(CommonFileMetadata.file_id == common_file.id)
            .with_for_update()
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
        distance = self._distance(metadata, place)
        if place is not None:
            self._set_photo_category_normal(
                common_file=common_file,
                metadata=metadata,
            )
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
        state = self.db.get(MemoryKeeperFileState, file_id)
        # A user's explicit Place decision or DAILY classification is authoritative.
        if blocks_automatic_place_change(metadata, state):
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
        for common_file, metadata, state in self.repository.memorykeeper_files_with_gps_and_state():
            scanned += 1
            if blocks_automatic_place_change(metadata, state):
                unchanged += 1
                continue
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
        if place is not None:
            locked_place = self.repository.get(place.id, lock=True)
            if locked_place is None or not locked_place.active:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "PLACE_NOT_ASSIGNABLE",
                        "place_id": place.id,
                    },
                )
            place = locked_place
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

    def _set_photo_category_normal(
        self,
        *,
        common_file: CommonFile,
        metadata: CommonFileMetadata | None,
    ) -> None:
        """Manual Place assignment makes the photo exclusively NORMAL."""
        state = self.db.get(MemoryKeeperFileState, common_file.id)
        if state is None:
            service_link = FileServiceRepository(self.db).get(
                file_id=common_file.id,
                service_name=self.SERVICE_NAME,
            )
            if service_link is None:
                raise ValueError("MemoryKeeper service link is required")
            state = MemoryKeeperCaptureDateService(self.db).synchronize(
                common_file=common_file,
                service_link=service_link,
                metadata=metadata,
                state_missing_known=True,
                initial_favorite=bool(common_file.favorite),
            )
        if state.photo_category == MemoryKeeperPhotoCategory.NORMAL:
            return

        old_category = state.photo_category or MemoryKeeperPhotoCategory.NORMAL
        state.photo_category = MemoryKeeperPhotoCategory.NORMAL
        state.photo_category_revision = int(state.photo_category_revision or 0) + 1
        state.updated_at = datetime.now(timezone.utc)
        self.history.create_histories(
            items=[
                {
                    "file_id": common_file.id,
                    "field_name": "memorykeeper_photo_category",
                    "old_value": old_category,
                    "new_value": MemoryKeeperPhotoCategory.NORMAL,
                    "source": "USER",
                    "priority": MetadataPriority.USER,
                    "modified_by": self.__class__.__name__,
                    "approved": True,
                }
            ],
            commit=False,
        )
        self.changes.append(
            service_name=self.SERVICE_NAME,
            resource_type=self.PHOTO_CATEGORY_RESOURCE,
            resource_id=common_file.file_id,
            operation=ChangeOperation.UPDATE,
            revision=state.photo_category_revision,
        )

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

    @classmethod
    def _normalized_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = cls._normalized_name(value)
        return normalized or None

    @staticmethod
    def _merge_blocking_reasons(
        source: MemoryKeeperPlace,
        target: MemoryKeeperPlace,
    ) -> list[str]:
        reasons = []
        if source.id == target.id:
            reasons.append("SAME_PLACE")
        if source.deleted_at is not None:
            reasons.append("SOURCE_DELETED")
        elif not source.active:
            reasons.append("SOURCE_INACTIVE")
        if target.deleted_at is not None:
            reasons.append("TARGET_DELETED")
        elif not target.active:
            reasons.append("TARGET_INACTIVE")
        return reasons

    @staticmethod
    def _management_item(
        place: MemoryKeeperPlace,
        linked_photo_count: int,
    ) -> PlaceManagementItem:
        country = place.country
        region = place.city or place.province
        reason = location_hierarchy_reason(
            registered_place_id=place.id,
            country=country,
            region=region,
            place=place.display_name,
        )
        path = [
            str(value).strip()
            for value in (country, region, place.display_name)
            if value is not None and str(value).strip()
        ]
        return PlaceManagementItem(
            id=place.id,
            display_name=place.display_name,
            canonical_name=place.canonical_name,
            country=country,
            region=region,
            full_path=" > ".join(path),
            location_hierarchy_complete=reason is None,
            location_cleanup_reason=reason,
            address=place.address,
            latitude=place.latitude,
            longitude=place.longitude,
            radius_m=place.radius_m,
            active=place.active,
            revision=place.revision,
            linked_photo_count=linked_photo_count,
        )

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

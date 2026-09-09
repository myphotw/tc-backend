from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException, status
from sqlalchemy.orm import Query, Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.schemas.file import (
    MemoryKeeperBatchAssignPlaceRequest,
    MemoryKeeperBatchAssignPlaceResponse,
    MemoryKeeperPlaceStateItem,
    MemoryKeeperPlaceStateQueryRequest,
    MemoryKeeperPlaceStateQueryResponse,
)
from app.memorykeeper.schemas.place import FilePlaceResponse
from app.memorykeeper.services.place_matcher import PlaceMatchSource
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService


class MemoryKeeperFilePlaceBatchService:
    """Set-based place snapshots and atomic manual assignment for files."""

    SERVICE_NAME = "MemoryKeeper"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.places = MemoryKeeperPlaceService(db)

    def query_states(
        self,
        payload: MemoryKeeperPlaceStateQueryRequest,
    ) -> MemoryKeeperPlaceStateQueryResponse:
        rows = (
            self.db.query(CommonFile, CommonFileMetadata)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .outerjoin(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
            .filter(CommonFile.file_id.in_(payload.file_ids))
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .all()
        )
        by_public_id = {
            common_file.file_id: (common_file, metadata)
            for common_file, metadata in rows
        }
        self._raise_missing(payload.file_ids, by_public_id)

        return MemoryKeeperPlaceStateQueryResponse(
            items=[
                MemoryKeeperPlaceStateItem(
                    file_id=public_id,
                    common_file_id=by_public_id[public_id][0].id,
                    gps_lat=(
                        by_public_id[public_id][1].gps_lat
                        if by_public_id[public_id][1] is not None
                        else None
                    ),
                    gps_lon=(
                        by_public_id[public_id][1].gps_lon
                        if by_public_id[public_id][1] is not None
                        else None
                    ),
                    memorykeeper_place_id=(
                        by_public_id[public_id][1].memorykeeper_place_id
                        if by_public_id[public_id][1] is not None
                        else None
                    ),
                    place_match_revision=(
                        int(by_public_id[public_id][1].place_match_revision or 0)
                        if by_public_id[public_id][1] is not None
                        else 0
                    ),
                )
                for public_id in payload.file_ids
            ]
        )

    def assign_place(
        self,
        payload: MemoryKeeperBatchAssignPlaceRequest,
    ) -> MemoryKeeperBatchAssignPlaceResponse:
        try:
            place = self.places.get(str(payload.memorykeeper_place_id))
            if not place.active:
                raise HTTPException(
                    status_code=422,
                    detail="Inactive place cannot be assigned",
                )

            rows = self._file_query(payload.file_ids, lock=True).all()
            by_public_id = {common_file.file_id: common_file for common_file in rows}
            self._raise_missing(payload.file_ids, by_public_id)

            metadata_rows = (
                self.db.query(CommonFileMetadata)
                .filter(CommonFileMetadata.file_id.in_([row.id for row in rows]))
                .order_by(CommonFileMetadata.file_id.asc())
                .with_for_update()
                .all()
            )
            metadata_by_file = {metadata.file_id: metadata for metadata in metadata_rows}

            conflicts: list[dict[str, object]] = []
            for public_id in payload.file_ids:
                common_file = by_public_id[public_id]
                metadata = metadata_by_file.get(common_file.id)
                current_revision = (
                    int(metadata.place_match_revision or 0) if metadata else 0
                )
                expected_revision = payload.expected_place_revisions[public_id]
                if current_revision != expected_revision:
                    conflicts.append(
                        {
                            "file_id": public_id,
                            "expected_revision": expected_revision,
                            "current_revision": current_revision,
                        }
                    )
            if conflicts:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "REVISION_CONFLICT", "files": conflicts},
                )

            responses: list[FilePlaceResponse] = []
            for public_id in payload.file_ids:
                common_file = by_public_id[public_id]
                metadata = metadata_by_file.get(common_file.id)
                if metadata is None:
                    metadata = CommonFileMetadata(file_id=common_file.id)
                    self.db.add(metadata)
                    self.db.flush()
                    metadata_by_file[common_file.id] = metadata
                self.places._set_relation(
                    metadata=metadata,
                    common_file=common_file,
                    place=place,
                    source=PlaceMatchSource.USER,
                    distance_m=self.places._distance(metadata, place),
                )
                responses.append(
                    FilePlaceResponse(
                        file_id=common_file.file_id,
                        memorykeeper_place_id=metadata.memorykeeper_place_id,
                        place_display_name=place.display_name,
                        place_canonical_name=place.canonical_name,
                        geocoded_place_name=metadata.place_name,
                        place_match_source=metadata.place_match_source,
                        place_match_distance_m=metadata.place_match_distance_m,
                        place_revision=int(metadata.place_match_revision or 0),
                    )
                )

            result = MemoryKeeperBatchAssignPlaceResponse(
                items=responses,
                assigned_count=len(responses),
            )
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def _file_query(self, file_ids: list[str], *, lock: bool) -> Query:
        query = (
            self.db.query(CommonFile)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonFile.file_id.in_(file_ids))
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .order_by(CommonFile.id.asc())
        )
        if lock:
            query = query.with_for_update(of=CommonFile)
        return query

    @staticmethod
    def _raise_missing(
        file_ids: list[str],
        by_public_id: Mapping[str, object],
    ) -> None:
        missing = [file_id for file_id in file_ids if file_id not in by_public_id]
        if missing:
            raise HTTPException(
                status_code=404,
                detail={"code": "MEMORYKEEPER_FILES_NOT_FOUND", "file_ids": missing},
            )

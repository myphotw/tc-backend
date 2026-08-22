from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.services.gallery_media import build_gallery_media_url
from app.memorykeeper.schemas.pending import (
    PendingAssignPlaceRequest,
    PendingAssignPlaceResponse,
    PendingFileItem,
    PendingListResponse,
)
from app.memorykeeper.services.place_matcher import PlaceMatchSource
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService


class MemoryKeeperPendingService:
    SERVICE_NAME = "MemoryKeeper"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.places = MemoryKeeperPlaceService(db)

    def list(
        self,
        *,
        page: int,
        page_size: int,
        include_suggestions: bool = False,
    ) -> PendingListResponse:
        query = (
            self.db.query(CommonFile, CommonFileMetadata)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .outerjoin(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .filter(CommonFileMetadata.memorykeeper_place_id.is_(None))
        )
        total = query.count()
        rows = (
            query.order_by(
                CommonFileMetadata.datetime_original.desc().nullslast(),
                CommonFile.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        items: list[PendingFileItem] = []
        for common_file, metadata in rows:
            match = None
            if (
                include_suggestions
                and metadata is not None
                and metadata.gps_lat is not None
                and metadata.gps_lon is not None
            ):
                match = self.places.matcher.match(
                    gps_lat=float(metadata.gps_lat),
                    gps_lon=float(metadata.gps_lon),
                    canonical_name=metadata.place_name,
                )
            items.append(
                PendingFileItem(
                    file_id=common_file.file_id,
                    thumbnail_url=build_gallery_media_url(
                        common_file.file_id,
                        "thumbnail",
                        common_file.thumb_path,
                    ),
                    capture_datetime=metadata.datetime_original if metadata else None,
                    gps_lat=metadata.gps_lat if metadata else None,
                    gps_lon=metadata.gps_lon if metadata else None,
                    country=metadata.country if metadata else None,
                    province=metadata.province if metadata else None,
                    city=metadata.city if metadata else None,
                    district=metadata.district if metadata else None,
                    place_name=metadata.place_name if metadata else None,
                    memorykeeper_place_id=None,
                    place_revision=int(metadata.place_match_revision or 0) if metadata else 0,
                    suggested_place_id=match.place.id if match and match.place else None,
                    suggested_place_name=match.place.display_name if match and match.place else None,
                    suggested_match_source=match.source if match and match.place else None,
                )
            )
        return PendingListResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
        )

    def assign_place(self, payload: PendingAssignPlaceRequest) -> PendingAssignPlaceResponse:
        place = self.places.get(str(payload.memorykeeper_place_id))
        if not place.active:
            raise HTTPException(status_code=422, detail="Inactive place cannot be assigned")

        rows = (
            self.db.query(CommonFile)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonFile.file_id.in_(payload.file_ids))
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .order_by(CommonFile.id.asc())
            .with_for_update()
            .all()
        )
        by_public_id = {item.file_id: item for item in rows}
        missing = [file_id for file_id in payload.file_ids if file_id not in by_public_id]
        if missing:
            raise HTTPException(
                status_code=404,
                detail={"code": "MEMORYKEEPER_FILES_NOT_FOUND", "file_ids": missing},
            )

        metadata_by_file: dict[int, CommonFileMetadata] = {
            item.file_id: item
            for item in (
                self.db.query(CommonFileMetadata)
                .filter(CommonFileMetadata.file_id.in_([row.id for row in rows]))
                .all()
            )
        }
        conflicts: list[dict[str, object]] = []
        not_pending: list[str] = []
        for public_id in payload.file_ids:
            common_file = by_public_id[public_id]
            metadata = metadata_by_file.get(common_file.id)
            current_revision = int(metadata.place_match_revision or 0) if metadata else 0
            expected = payload.expected_revisions[public_id]
            if current_revision != expected:
                conflicts.append(
                    {
                        "file_id": public_id,
                        "expected_revision": expected,
                        "current_revision": current_revision,
                    }
                )
            if metadata is not None and metadata.memorykeeper_place_id is not None:
                not_pending.append(public_id)
        if conflicts:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "REVISION_CONFLICT", "files": conflicts},
            )
        if not_pending:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "FILES_NOT_PENDING", "file_ids": not_pending},
            )

        responses = []
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
            responses.append(self.places.file_place_response(common_file, metadata))
        self.db.commit()
        return PendingAssignPlaceResponse(items=responses, assigned_count=len(responses))

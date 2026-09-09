from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.astrojournal.models.observation_site import (
    AstroObservationSite,
    AstroObservationSiteBlockedRange,
    AstroObservationSiteHorizonPoint,
)
from app.astrojournal.repositories.equipment_repository import EquipmentRepository
from app.astrojournal.repositories.observation_site_repository import (
    ObservationSiteRepository,
)
from app.astrojournal.schemas.observation_site import (
    BlockedAzimuthRangeResponse,
    BlockedAzimuthRangeWrite,
    HorizonPointResponse,
    HorizonPointWrite,
    ObservationSiteCreate,
    ObservationSiteDeleteResponse,
    ObservationSiteResponse,
    ObservationSiteUpdate,
)
from app.common.repositories.change_event_repository import (
    ChangeEventRepository,
    ChangeOperation,
)


class ObservationSiteService:
    SERVICE_NAME = "AstroJournal"
    RESOURCE_TYPE = "ObservationSite"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = ObservationSiteRepository(db)
        self.equipment = EquipmentRepository(db)
        self.changes = ChangeEventRepository(db)

    def create(self, payload: ObservationSiteCreate) -> ObservationSiteResponse:
        site_id = str(payload.id)
        existing = self.repository.get(site_id, include_deleted=True)
        if existing is not None:
            if existing.deleted_at is not None:
                self._deleted_id_conflict(site_id)
            return self._response(existing)
        self._require_equipment(payload.default_equipment_id)

        site = AstroObservationSite(
            id=site_id,
            **self._parent_values(payload),
        )
        try:
            self.repository.create(site)
            self.repository.replace_horizon_points(
                site.id,
                self._horizon_values(payload.horizon_points),
            )
            self.repository.replace_blocked_ranges(
                site.id,
                self._blocked_values(payload.blocked_azimuth_ranges),
            )
            self._append_change(site, ChangeOperation.CREATE)
            self.db.commit()
            self.db.refresh(site)
            return self._response(site)
        except IntegrityError:
            self.db.rollback()
            existing = self.repository.get(site_id, include_deleted=True)
            if existing is not None and existing.deleted_at is None:
                return self._response(existing)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "OBSERVATION_SITE_AGGREGATE_CONFLICT"},
            )
        except Exception:
            self.db.rollback()
            raise

    def get(self, site_id: str) -> ObservationSiteResponse:
        site = self.repository.get(site_id)
        if site is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Observation site not found",
            )
        return self._response(site)

    def list(self) -> list[ObservationSiteResponse]:
        sites = self.repository.list()
        site_ids = [site.id for site in sites]
        horizon = self.repository.horizon_points(site_ids)
        blocked = self.repository.blocked_ranges(site_ids)
        return [
            self._response(
                site,
                horizon_points=horizon.get(site.id, []),
                blocked_ranges=blocked.get(site.id, []),
            )
            for site in sites
        ]

    def update(
        self,
        site_id: str,
        payload: ObservationSiteUpdate,
    ) -> ObservationSiteResponse:
        try:
            site = self.repository.get(site_id, lock=True)
            if site is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Observation site not found",
                )
            if site.revision != payload.expected_revision:
                self._revision_conflict(site, payload.expected_revision)

            current = self._response(site)
            values = current.model_dump(
                exclude={"revision", "created_at", "updated_at", "deleted_at"}
            )
            values.update(
                payload.model_dump(
                    exclude={"expected_revision"},
                    exclude_unset=True,
                )
            )
            candidate = ObservationSiteCreate.model_validate(values)
            self._require_equipment(candidate.default_equipment_id)

            for name, value in self._parent_values(candidate).items():
                setattr(site, name, value)
            if "horizon_points" in payload.model_fields_set:
                self.repository.replace_horizon_points(
                    site.id,
                    self._horizon_values(candidate.horizon_points),
                )
            if "blocked_azimuth_ranges" in payload.model_fields_set:
                self.repository.replace_blocked_ranges(
                    site.id,
                    self._blocked_values(candidate.blocked_azimuth_ranges),
                )
            site.revision += 1
            site.updated_at = datetime.now(timezone.utc)
            self._append_change(site, ChangeOperation.UPDATE)
            self.db.commit()
            self.db.refresh(site)
            return self._response(site)
        except IntegrityError:
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "OBSERVATION_SITE_AGGREGATE_CONFLICT"},
            )
        except Exception:
            self.db.rollback()
            raise

    def soft_delete(
        self,
        site_id: str,
        *,
        expected_revision: int,
    ) -> ObservationSiteDeleteResponse:
        try:
            site = self.repository.get(site_id, lock=True)
            if site is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Observation site not found",
                )
            if site.revision != expected_revision:
                self._revision_conflict(site, expected_revision)
            changed_at = datetime.now(timezone.utc)
            self.repository.delete_children(site.id)
            site.deleted_at = changed_at
            site.updated_at = changed_at
            site.revision += 1
            self._append_change(site, ChangeOperation.DELETE, tombstone=True)
            result = ObservationSiteDeleteResponse(
                site_id=site.id,
                deleted=True,
                revision=site.revision,
                deleted_at=site.deleted_at,
            )
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def _response(
        self,
        site: AstroObservationSite,
        *,
        horizon_points: list[AstroObservationSiteHorizonPoint] | None = None,
        blocked_ranges: list[AstroObservationSiteBlockedRange] | None = None,
    ) -> ObservationSiteResponse:
        if horizon_points is None:
            horizon_points = self.repository.horizon_points([site.id]).get(site.id, [])
        if blocked_ranges is None:
            blocked_ranges = self.repository.blocked_ranges([site.id]).get(site.id, [])
        return ObservationSiteResponse.model_validate(
            {
                **{
                    column.name: getattr(site, column.name)
                    for column in site.__table__.columns
                },
                "horizon_points": [
                    HorizonPointResponse.model_validate(item, from_attributes=True)
                    for item in horizon_points
                ],
                "blocked_azimuth_ranges": [
                    BlockedAzimuthRangeResponse.model_validate(
                        item,
                        from_attributes=True,
                    )
                    for item in blocked_ranges
                ],
            }
        )

    def _require_equipment(self, equipment_id: UUID | None) -> None:
        if equipment_id is None:
            return
        if self.equipment.get(str(equipment_id)) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Default equipment not found",
            )

    @staticmethod
    def _parent_values(payload: ObservationSiteCreate) -> dict[str, object]:
        values = payload.model_dump(
            exclude={"id", "horizon_points", "blocked_azimuth_ranges"}
        )
        if payload.default_equipment_id is not None:
            values["default_equipment_id"] = str(payload.default_equipment_id)
        return values

    @staticmethod
    def _horizon_values(
        items: list[HorizonPointWrite],
    ) -> list[dict[str, object]]:
        return [
            {
                **item.model_dump(exclude={"observation_site_id"}),
                "id": str(item.id),
            }
            for item in items
        ]

    @staticmethod
    def _blocked_values(
        items: list[BlockedAzimuthRangeWrite],
    ) -> list[dict[str, object]]:
        return [
            {
                **item.model_dump(exclude={"observation_site_id"}),
                "id": str(item.id),
            }
            for item in items
        ]

    def _append_change(
        self,
        site: AstroObservationSite,
        operation: str,
        *,
        tombstone: bool = False,
    ) -> None:
        self.changes.append(
            service_name=self.SERVICE_NAME,
            resource_type=self.RESOURCE_TYPE,
            resource_id=site.id,
            operation=operation,
            revision=site.revision,
            tombstone=tombstone,
        )

    @staticmethod
    def _revision_conflict(
        site: AstroObservationSite,
        expected_revision: int,
    ) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REVISION_CONFLICT",
                "site_id": site.id,
                "expected_revision": expected_revision,
                "current_revision": site.revision,
            },
        )

    @staticmethod
    def _deleted_id_conflict(site_id: str) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RESOURCE_TOMBSTONED",
                "site_id": site_id,
            },
        )

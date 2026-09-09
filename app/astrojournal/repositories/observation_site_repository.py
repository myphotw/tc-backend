from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from app.astrojournal.models.observation_site import (
    AstroObservationSite,
    AstroObservationSiteBlockedRange,
    AstroObservationSiteHorizonPoint,
)


class ObservationSiteRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, site: AstroObservationSite) -> AstroObservationSite:
        self.db.add(site)
        self.db.flush()
        return site

    def get(
        self,
        site_id: str,
        *,
        include_deleted: bool = False,
        lock: bool = False,
    ) -> AstroObservationSite | None:
        query = self.db.query(AstroObservationSite).filter(
            AstroObservationSite.id == site_id
        )
        if not include_deleted:
            query = query.filter(AstroObservationSite.deleted_at.is_(None))
        if lock:
            query = query.with_for_update()
        return query.first()

    def list(self) -> list[AstroObservationSite]:
        return (
            self.db.query(AstroObservationSite)
            .filter(AstroObservationSite.deleted_at.is_(None))
            .order_by(
                AstroObservationSite.is_favorite.desc(),
                AstroObservationSite.name.asc(),
                AstroObservationSite.id.asc(),
            )
            .all()
        )

    def horizon_points(
        self,
        site_ids: list[str],
    ) -> dict[str, list[AstroObservationSiteHorizonPoint]]:
        grouped: dict[str, list[AstroObservationSiteHorizonPoint]] = defaultdict(list)
        if not site_ids:
            return grouped
        rows = (
            self.db.query(AstroObservationSiteHorizonPoint)
            .filter(
                AstroObservationSiteHorizonPoint.observation_site_id.in_(site_ids)
            )
            .order_by(
                AstroObservationSiteHorizonPoint.observation_site_id.asc(),
                AstroObservationSiteHorizonPoint.sort_order.asc(),
                AstroObservationSiteHorizonPoint.azimuth.asc(),
                AstroObservationSiteHorizonPoint.id.asc(),
            )
            .all()
        )
        for row in rows:
            grouped[row.observation_site_id].append(row)
        return grouped

    def blocked_ranges(
        self,
        site_ids: list[str],
    ) -> dict[str, list[AstroObservationSiteBlockedRange]]:
        grouped: dict[str, list[AstroObservationSiteBlockedRange]] = defaultdict(list)
        if not site_ids:
            return grouped
        rows = (
            self.db.query(AstroObservationSiteBlockedRange)
            .filter(
                AstroObservationSiteBlockedRange.observation_site_id.in_(site_ids)
            )
            .order_by(
                AstroObservationSiteBlockedRange.observation_site_id.asc(),
                AstroObservationSiteBlockedRange.start_azimuth.asc(),
                AstroObservationSiteBlockedRange.end_azimuth.asc(),
                AstroObservationSiteBlockedRange.id.asc(),
            )
            .all()
        )
        for row in rows:
            grouped[row.observation_site_id].append(row)
        return grouped

    def replace_horizon_points(
        self,
        site_id: str,
        values: list[dict[str, object]],
    ) -> None:
        existing = (
            self.db.query(AstroObservationSiteHorizonPoint)
            .filter(AstroObservationSiteHorizonPoint.observation_site_id == site_id)
            .all()
        )
        for child in existing:
            self.db.delete(child)
        self.db.flush()
        for item in values:
            child_values = dict(item)
            child_id = str(child_values.pop("id"))
            self.db.add(
                AstroObservationSiteHorizonPoint(
                    id=child_id,
                    observation_site_id=site_id,
                    **child_values,
                )
            )
        self.db.flush()

    def replace_blocked_ranges(
        self,
        site_id: str,
        values: list[dict[str, object]],
    ) -> None:
        existing = (
            self.db.query(AstroObservationSiteBlockedRange)
            .filter(AstroObservationSiteBlockedRange.observation_site_id == site_id)
            .all()
        )
        for child in existing:
            self.db.delete(child)
        self.db.flush()
        for item in values:
            child_values = dict(item)
            child_id = str(child_values.pop("id"))
            self.db.add(
                AstroObservationSiteBlockedRange(
                    id=child_id,
                    observation_site_id=site_id,
                    **child_values,
                )
            )
        self.db.flush()

    def delete_children(self, site_id: str) -> None:
        self.replace_horizon_points(site_id, [])
        self.replace_blocked_ranges(site_id, [])

    def active_sites_using_equipment(
        self,
        equipment_id: str,
    ) -> list[AstroObservationSite]:
        return (
            self.db.query(AstroObservationSite)
            .filter(AstroObservationSite.deleted_at.is_(None))
            .filter(AstroObservationSite.default_equipment_id == equipment_id)
            .order_by(AstroObservationSite.id.asc())
            .with_for_update()
            .all()
        )

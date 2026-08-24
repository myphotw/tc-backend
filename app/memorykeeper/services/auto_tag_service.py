from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Query, Session

from app.common.config import settings
from app.common.models.api_usage import CommonApiUsage
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.api_usage_repository import (
    ApiName,
    ApiProvider,
    ApiUsageRepository,
)
from app.common.repositories.tag_repository import TagSource
from app.common.repositories.vision_job_repository import VisionJobStatus
from app.common.repositories.worker_status_repository import WorkerStatusRepository
from app.memorykeeper.schemas.auto_tag import (
    AutoTagCurationPreviewResponse,
    AutoTagFailedJobItem,
    AutoTagFailedJobListResponse,
    AutoTagRetryResponse,
    AutoTagStatusResponse,
)
from app.memorykeeper.services.tag_curation_service import (
    MemoryKeeperTagCurationService,
    RawTagInput,
)


class MemoryKeeperAutoTagService:
    SERVICE_NAME = "MemoryKeeper"
    WORKER_NAME = "VisionWorker"
    MAX_RETRY_COUNT = 3

    def __init__(self, db: Session) -> None:
        self.db = db
        self.curation = MemoryKeeperTagCurationService()

    def status(self) -> AutoTagStatusResponse:
        counts = {
            status: self._job_query().filter(CommonVisionJob.status == status).count()
            for status in (
                VisionJobStatus.WAITING,
                VisionJobStatus.PROCESSING,
                VisionJobStatus.FAILED,
            )
        }
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_completed = (
            self._job_query()
            .filter(CommonVisionJob.status == VisionJobStatus.COMPLETED)
            .filter(CommonVisionJob.completed_at >= today_start)
            .count()
        )
        last_processed = (
            self._job_query()
            .filter(CommonVisionJob.status == VisionJobStatus.COMPLETED)
            .with_entities(func.max(CommonVisionJob.completed_at))
            .scalar()
        )
        last_failure = (
            self._job_query()
            .filter(CommonVisionJob.status == VisionJobStatus.FAILED)
            .with_entities(func.max(CommonVisionJob.completed_at))
            .scalar()
        )
        usage = self._current_usage(now)
        monthly_usage = int(usage.used_unit or 0) if usage is not None else 0
        monthly_limit = ApiUsageRepository.effective_limit(ApiName.VISION)
        monthly_remaining = max(0, monthly_limit - monthly_usage)
        quota_available = monthly_remaining > 0
        credential_ready = self._credential_ready()
        worker_repo = WorkerStatusRepository(self.db)
        worker = worker_repo.get_worker(self.WORKER_NAME)
        worker_online = bool(
            worker is not None
            and worker_repo.resolve_display_status(worker) == "RUNNING"
        )
        return AutoTagStatusResponse(
            service_available=credential_ready and worker_online,
            credential_ready=credential_ready,
            worker_online=worker_online,
            quota_available=quota_available,
            monthly_limit_reached=not quota_available,
            quota_waiting_count=(
                counts[VisionJobStatus.WAITING] if not quota_available else 0
            ),
            waiting_count=counts[VisionJobStatus.WAITING],
            processing_count=counts[VisionJobStatus.PROCESSING],
            failed_count=counts[VisionJobStatus.FAILED],
            today_completed_count=today_completed,
            monthly_usage=monthly_usage,
            monthly_limit=monthly_limit,
            monthly_remaining=monthly_remaining,
            curation_version=self.curation.CURATION_VERSION,
            last_processed_at=last_processed,
            last_failure_at=last_failure,
        )

    def failed_jobs(
        self,
        *,
        page: int,
        page_size: int,
    ) -> AutoTagFailedJobListResponse:
        query = self._job_file_query().filter(
            CommonVisionJob.status == VisionJobStatus.FAILED
        )
        total = query.count()
        rows = (
            query.order_by(
                CommonVisionJob.completed_at.desc(),
                CommonVisionJob.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return AutoTagFailedJobListResponse(
            items=[self._failed_item(job, common_file) for job, common_file in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def retry_failed(self, *, limit: int) -> AutoTagRetryResponse:
        jobs = (
            self._job_query()
            .filter(CommonVisionJob.status == VisionJobStatus.FAILED)
            .order_by(CommonVisionJob.completed_at.asc(), CommonVisionJob.id.asc())
            .with_for_update(skip_locked=True)
            .limit(limit)
            .all()
        )
        return self._requeue(jobs, requested_count=len(jobs))

    def retry_job(self, job_id: int) -> AutoTagRetryResponse:
        job = (
            self._job_query()
            .filter(CommonVisionJob.id == job_id)
            .with_for_update()
            .first()
        )
        if job is None:
            raise HTTPException(status_code=404, detail="MemoryKeeper Vision job not found")
        return self._requeue([job], requested_count=1)

    def curation_preview(self, *, sample_limit: int) -> AutoTagCurationPreviewResponse:
        raw_base = (
            self.db.query(CommonFileTag)
            .join(CommonFile, CommonFile.id == CommonFileTag.file_id)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .filter(CommonFileTag.source == TagSource.AI)
            .filter(CommonFileTag.deleted.is_(False))
        )
        files_with_raw_tags = raw_base.with_entities(
            func.count(func.distinct(CommonFileTag.file_id))
        ).scalar() or 0
        current_raw_tag_count = raw_base.count()
        sampled = (
            raw_base.with_entities(CommonFileTag.file_id)
            .distinct()
            .order_by(CommonFileTag.file_id.asc())
            .limit(sample_limit + 1)
            .all()
        )
        sample_ids = [int(row[0]) for row in sampled[:sample_limit]]
        has_more = len(sampled) > sample_limit
        relations = (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.file_id.in_(sample_ids))
            .filter(
                or_(
                    CommonFileTag.source == TagSource.USER,
                    CommonFileTag.deleted.is_(False),
                )
            )
            .all()
            if sample_ids
            else []
        )
        metadata = (
            {
                item.file_id: item
                for item in self.db.query(CommonFileMetadata)
                .filter(CommonFileMetadata.file_id.in_(sample_ids))
                .all()
            }
            if sample_ids
            else {}
        )
        by_file: dict[int, list[CommonFileTag]] = defaultdict(list)
        for relation in relations:
            by_file[relation.file_id].append(relation)

        projected_count = 0
        zero_count = 0
        sampled_raw_count = 0
        mapped_count = 0
        for file_id in sample_ids:
            file_relations = by_file.get(file_id, [])
            raw = [
                relation
                for relation in file_relations
                if relation.source == TagSource.AI and not relation.deleted
            ]
            sampled_raw_count += len(raw)
            mapped_count += sum(
                1
                for relation in raw
                if float(relation.confidence or 0) >= self.curation.CONFIDENCE_THRESHOLD
                and self.curation.rule_for(relation.tag) is not None
            )
            result = self.curation.curate(
                [RawTagInput(item.tag, item.confidence) for item in raw],
                user_tags=[
                    item.tag
                    for item in file_relations
                    if item.source == TagSource.USER
                ],
                structured_terms=self._structured_terms(metadata.get(file_id)),
            )
            projected_count += len(result.tags)
            if not result.tags:
                zero_count += 1

        mapped_percentage = (
            round(mapped_count / sampled_raw_count * 100, 2)
            if sampled_raw_count
            else 0.0
        )
        return AutoTagCurationPreviewResponse(
            service_name=self.SERVICE_NAME,
            curation_version=self.curation.CURATION_VERSION,
            files_with_raw_tags=int(files_with_raw_tags),
            current_raw_tag_count=int(current_raw_tag_count),
            evaluated_file_count=len(sample_ids),
            projected_curated_tag_count=projected_count,
            zero_tag_file_count=zero_count,
            mapped_percentage=mapped_percentage,
            sample_limit=sample_limit,
            has_more=has_more,
        )

    def _requeue(
        self,
        jobs: list[CommonVisionJob],
        *,
        requested_count: int,
    ) -> AutoTagRetryResponse:
        requeued = 0
        skipped = 0
        still_failed = 0
        now = datetime.now(timezone.utc)
        for job in jobs:
            if job.status != VisionJobStatus.FAILED:
                skipped += 1
                continue
            code = self._safe_error_code(job.last_error)
            if int(job.retry_count or 0) >= self.MAX_RETRY_COUNT or code == "FILE_UNAVAILABLE":
                skipped += 1
                still_failed += 1
                continue
            job.status = VisionJobStatus.WAITING
            job.requested_at = now
            job.started_at = None
            job.completed_at = None
            job.last_error = None
            requeued += 1
        if requeued:
            self.db.commit()
        return AutoTagRetryResponse(
            requested_count=requested_count,
            requeued_count=requeued,
            skipped_count=skipped,
            failed_count=still_failed,
        )

    def _job_query(self) -> Query:
        return (
            self.db.query(CommonVisionJob)
            .join(CommonFile, CommonFile.id == CommonVisionJob.file_id)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonVisionJob.deleted.is_(False))
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
        )

    def _job_file_query(self) -> Query:
        return (
            self.db.query(CommonVisionJob, CommonFile)
            .join(CommonFile, CommonFile.id == CommonVisionJob.file_id)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonVisionJob.deleted.is_(False))
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
        )

    def _current_usage(self, now: datetime) -> CommonApiUsage | None:
        return (
            self.db.query(CommonApiUsage)
            .filter(CommonApiUsage.deleted.is_(False))
            .filter(CommonApiUsage.provider == ApiProvider.GOOGLE)
            .filter(CommonApiUsage.api_name == ApiName.VISION)
            .filter(CommonApiUsage.year == now.year)
            .filter(CommonApiUsage.month == now.month)
            .first()
        )

    def _failed_item(
        self,
        job: CommonVisionJob,
        common_file: CommonFile,
    ) -> AutoTagFailedJobItem:
        code = self._safe_error_code(job.last_error)
        return AutoTagFailedJobItem(
            job_id=job.id,
            file_id=common_file.file_id,
            failed_at=job.completed_at or job.updated_at or job.created_at,
            retry_count=int(job.retry_count or 0),
            safe_error_code=code,
            retryable=(
                int(job.retry_count or 0) < self.MAX_RETRY_COUNT
                and code != "FILE_UNAVAILABLE"
            ),
        )

    @staticmethod
    def _credential_ready() -> bool:
        path = settings.GOOGLE_VISION_CREDENTIAL
        if not path:
            return False
        try:
            return Path(path).is_file()
        except (OSError, ValueError):
            return False

    @staticmethod
    def _safe_error_code(error: str | None) -> str:
        normalized = (error or "").casefold()
        if "usage limit" in normalized or "quota" in normalized:
            return "USAGE_LIMIT"
        if any(value in normalized for value in ("credential", "service account", "auth")):
            return "CREDENTIAL_UNAVAILABLE"
        if any(
            value in normalized
            for value in ("commonfile not found", "original image path", "no such file")
        ):
            return "FILE_UNAVAILABLE"
        if any(value in normalized for value in ("timeout", "connection", "network")):
            return "NETWORK_ERROR"
        if "vision api responded" in normalized or "provider" in normalized:
            return "PROVIDER_ERROR"
        return "PROCESSING_ERROR"

    @staticmethod
    def _structured_terms(metadata: CommonFileMetadata | None) -> list[str]:
        if metadata is None:
            return []
        values = (
            metadata.country,
            metadata.province,
            metadata.city,
            metadata.district,
            metadata.place_name,
        )
        terms = [str(value) for value in values if value]
        if metadata.datetime_original is not None:
            terms.append(str(metadata.datetime_original.year))
        return terms

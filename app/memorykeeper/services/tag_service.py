from __future__ import annotations

from datetime import datetime, timezone
import re
import unicodedata

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.repositories.change_event_repository import ChangeEventRepository, ChangeOperation
from app.common.repositories.tag_repository import TagSource, TagType
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.tag import Tag
from app.memorykeeper.schemas.file import FileTagMutationResponse
from app.memorykeeper.schemas.tag import (
    TagCreate,
    TagListResponse,
    TagMergeRequest,
    TagResponse,
    TagUpdate,
)
from app.memorykeeper.services.file_service import MemoryKeeperFileService


class MemoryKeeperTagService:
    SERVICE_NAME = "MemoryKeeper"
    TAG_RESOURCE = "MemoryKeeperTag"
    FILE_TAG_RESOURCE = "MemoryKeeperFileTag"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.changes = ChangeEventRepository(db)
        self.files = MemoryKeeperFileService(db)

    def list(
        self,
        *,
        query: str | None,
        favorite: bool | None,
        limit: int,
        offset: int,
    ) -> TagListResponse:
        rows = self.db.query(Tag).filter(Tag.deleted.is_(False))
        if query:
            rows = rows.filter(Tag.tag_name.ilike(f"%{query.strip()}%"))
        if favorite is not None:
            rows = rows.filter(Tag.favorite.is_(favorite))
        total = rows.count()
        tags = (
            rows.order_by(Tag.favorite.desc(), Tag.tag_name.asc(), Tag.id.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        usages = self._usage_counts([tag.id for tag in tags])
        return TagListResponse(
            items=[self._response(tag, usages.get(tag.id, 0)) for tag in tags],
            total=total,
        )

    def create(self, payload: TagCreate) -> TagResponse:
        normalized = self._normalize(payload.name)
        if self._find_normalized(normalized, include_deleted=True) is not None:
            self._duplicate_name(payload.name)
        tag = Tag(
            tag_name=payload.name,
            normalized_name=normalized,
            tag_type=TagType.USER,
            source=TagSource.USER,
            favorite=payload.favorite,
            revision=1,
            deleted=False,
        )
        self.db.add(tag)
        try:
            self.db.flush()
            self._append_tag_change(tag, ChangeOperation.CREATE)
            self.db.commit()
            self.db.refresh(tag)
        except IntegrityError:
            self.db.rollback()
            self._duplicate_name(payload.name)
        return self._response(tag, 0)

    def get(self, tag_id: int, *, lock: bool = False, include_deleted: bool = False) -> Tag:
        query = self.db.query(Tag).filter(Tag.id == tag_id)
        if not include_deleted:
            query = query.filter(Tag.deleted.is_(False))
        if lock:
            query = query.with_for_update()
        tag = query.first()
        if tag is None:
            raise HTTPException(status_code=404, detail="MemoryKeeper tag not found")
        return tag

    def update(self, tag_id: int, payload: TagUpdate) -> TagResponse:
        tag = self.get(tag_id, lock=True)
        self._check_revision(tag, payload.revision)
        values = payload.model_dump(exclude={"revision"}, exclude_unset=True)
        if "name" in values:
            normalized = self._normalize(values["name"])
            duplicate = self._find_normalized(normalized, include_deleted=True)
            if duplicate is not None and duplicate.id != tag.id:
                self._duplicate_name(values["name"])
            tag.tag_name = values["name"]
            tag.normalized_name = normalized
            (
                self.db.query(CommonFileTag)
                .filter(CommonFileTag.memorykeeper_tag_id == tag.id)
                .update({CommonFileTag.tag: tag.tag_name}, synchronize_session=False)
            )
        if "favorite" in values:
            tag.favorite = bool(values["favorite"])
        tag.revision += 1
        tag.updated_at = datetime.now(timezone.utc)
        try:
            self._append_tag_change(tag, ChangeOperation.UPDATE)
            self.db.commit()
            self.db.refresh(tag)
        except IntegrityError:
            self.db.rollback()
            self._duplicate_name(values.get("name", tag.tag_name))
        return self._response(tag, self._usage_count(tag.id))

    def delete(self, tag_id: int, *, expected_revision: int) -> None:
        tag = self.get(tag_id, lock=True)
        self._check_revision(tag, expected_revision)
        active_relations = (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.memorykeeper_tag_id == tag.id)
            .filter(CommonFileTag.deleted.is_(False))
            .all()
        )
        for relation in active_relations:
            relation.deleted = True
            revision = self._touch_file_revision(relation.file_id)
            self._append_file_tag_change(
                relation,
                ChangeOperation.DELETE,
                revision=revision,
                tombstone=True,
            )
        tag.deleted = True
        tag.revision += 1
        tag.updated_at = datetime.now(timezone.utc)
        self._append_tag_change(tag, ChangeOperation.DELETE, tombstone=True)
        self.db.commit()

    def merge(self, source_tag_id: int, payload: TagMergeRequest) -> TagResponse:
        if source_tag_id == payload.target_tag_id:
            raise HTTPException(status_code=422, detail="source and target tags must differ")
        tags = (
            self.db.query(Tag)
            .filter(Tag.id.in_([source_tag_id, payload.target_tag_id]))
            .filter(Tag.deleted.is_(False))
            .order_by(Tag.id.asc())
            .with_for_update()
            .all()
        )
        by_id = {tag.id: tag for tag in tags}
        source = by_id.get(source_tag_id)
        target = by_id.get(payload.target_tag_id)
        if source is None or target is None:
            raise HTTPException(status_code=404, detail="MemoryKeeper tag not found")
        self._check_revision(source, payload.source_revision)
        self._check_revision(target, payload.target_revision)

        source_relations = (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.memorykeeper_tag_id == source.id)
            .all()
        )
        for source_relation in source_relations:
            target_relation = (
                self.db.query(CommonFileTag)
                .filter(CommonFileTag.file_id == source_relation.file_id)
                .filter(CommonFileTag.memorykeeper_tag_id == target.id)
                .first()
            )
            if not source_relation.deleted:
                if target_relation is None:
                    target_relation = CommonFileTag(
                        file_id=source_relation.file_id,
                        memorykeeper_tag_id=target.id,
                        tag=target.tag_name,
                        tag_type=TagType.USER,
                        source=TagSource.USER,
                        confidence=None,
                        deleted=False,
                    )
                    self.db.add(target_relation)
                    self.db.flush()
                else:
                    target_relation.tag = target.tag_name
                    target_relation.tag_type = TagType.USER
                    target_relation.source = TagSource.USER
                    target_relation.confidence = None
                    target_relation.deleted = False
                revision = self._touch_file_revision(source_relation.file_id)
                self._append_file_tag_change(
                    target_relation,
                    ChangeOperation.UPDATE,
                    revision=revision,
                )
            source_relation.deleted = True

        source.deleted = True
        source.revision += 1
        source.updated_at = datetime.now(timezone.utc)
        target.revision += 1
        target.updated_at = datetime.now(timezone.utc)
        self._append_tag_change(source, ChangeOperation.DELETE, tombstone=True)
        self._append_tag_change(target, ChangeOperation.UPDATE)
        self.db.commit()
        self.db.refresh(target)
        return self._response(target, self._usage_count(target.id))

    def assign(
        self,
        public_file_id: str,
        tag_id: int,
        *,
        expected_revision: int,
    ) -> FileTagMutationResponse:
        common_file = self.files.require_file(public_file_id, lock=True)
        state = self.files.get_state(common_file, create=True)
        self._check_file_revision(common_file, state, expected_revision)
        tag = self.get(tag_id)

        normalized = self._normalize(tag.tag_name)
        for ai_tag in (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.file_id == common_file.id)
            .filter(CommonFileTag.source == TagSource.AI)
            .filter(CommonFileTag.deleted.is_(False))
            .all()
        ):
            if self._normalize(ai_tag.tag) == normalized:
                ai_tag.deleted = True

        relation = (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.file_id == common_file.id)
            .filter(CommonFileTag.memorykeeper_tag_id == tag.id)
            .first()
        )
        operation = ChangeOperation.UPDATE
        if relation is None:
            relation = CommonFileTag(
                file_id=common_file.id,
                memorykeeper_tag_id=tag.id,
                tag=tag.tag_name,
                tag_type=TagType.USER,
                source=TagSource.USER,
                confidence=None,
                deleted=False,
            )
            self.db.add(relation)
            self.db.flush()
            operation = ChangeOperation.CREATE
        else:
            relation.tag = tag.tag_name
            relation.tag_type = TagType.USER
            relation.source = TagSource.USER
            relation.confidence = None
            relation.deleted = False
        state.revision += 1
        state.updated_at = datetime.now(timezone.utc)
        self._append_file_tag_change(relation, operation, revision=state.revision)
        self.db.commit()
        return FileTagMutationResponse(
            file_id=common_file.file_id,
            tag_id=tag.id,
            assigned=True,
            revision=state.revision,
        )

    def remove(
        self,
        public_file_id: str,
        tag_id: int,
        *,
        expected_revision: int,
    ) -> FileTagMutationResponse:
        common_file = self.files.require_file(public_file_id, lock=True)
        state = self.files.get_state(common_file, create=True)
        self._check_file_revision(common_file, state, expected_revision)
        self.get(tag_id, include_deleted=True)
        relation = (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.file_id == common_file.id)
            .filter(CommonFileTag.memorykeeper_tag_id == tag_id)
            .filter(CommonFileTag.deleted.is_(False))
            .first()
        )
        if relation is None:
            raise HTTPException(status_code=404, detail="File tag relation not found")
        relation.deleted = True
        state.revision += 1
        state.updated_at = datetime.now(timezone.utc)
        self._append_file_tag_change(
            relation,
            ChangeOperation.DELETE,
            revision=state.revision,
            tombstone=True,
        )
        self.db.commit()
        return FileTagMutationResponse(
            file_id=common_file.file_id,
            tag_id=tag_id,
            assigned=False,
            revision=state.revision,
        )

    def _find_normalized(self, normalized: str, *, include_deleted: bool) -> Tag | None:
        query = self.db.query(Tag)
        if not include_deleted:
            query = query.filter(Tag.deleted.is_(False))
        direct = query.filter(Tag.normalized_name == normalized).first()
        if direct is not None:
            return direct
        for tag in query.filter(or_(Tag.normalized_name.is_(None), Tag.normalized_name == "")).all():
            if self._normalize(tag.tag_name) == normalized:
                return tag
        return None

    def _usage_counts(self, tag_ids: list[int]) -> dict[int, int]:
        if not tag_ids:
            return {}
        rows = (
            self.db.query(CommonFileTag.memorykeeper_tag_id, func.count(CommonFileTag.id))
            .join(CommonFile, CommonFile.id == CommonFileTag.file_id)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonFileTag.memorykeeper_tag_id.in_(tag_ids))
            .filter(CommonFileTag.deleted.is_(False))
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .group_by(CommonFileTag.memorykeeper_tag_id)
            .all()
        )
        return {int(tag_id): int(count) for tag_id, count in rows}

    def _usage_count(self, tag_id: int) -> int:
        return self._usage_counts([tag_id]).get(tag_id, 0)

    def _touch_file_revision(self, file_id: int) -> int | None:
        common_file = self.db.get(CommonFile, file_id)
        if common_file is None:
            return None
        state = self.files.get_state(common_file, create=True)
        state.revision += 1
        state.updated_at = datetime.now(timezone.utc)
        return int(state.revision)

    def _append_tag_change(self, tag: Tag, operation: str, *, tombstone: bool = False) -> None:
        self.changes.append(
            service_name=self.SERVICE_NAME,
            resource_type=self.TAG_RESOURCE,
            resource_id=str(tag.id),
            operation=operation,
            revision=tag.revision,
            tombstone=tombstone,
        )

    def _append_file_tag_change(
        self,
        relation: CommonFileTag,
        operation: str,
        *,
        revision: int | None = None,
        tombstone: bool = False,
    ) -> None:
        common_file = self.db.get(CommonFile, relation.file_id)
        resource_id = f"{common_file.file_id if common_file else relation.file_id}:{relation.memorykeeper_tag_id}"
        self.changes.append(
            service_name=self.SERVICE_NAME,
            resource_type=self.FILE_TAG_RESOURCE,
            resource_id=resource_id,
            operation=operation,
            revision=revision,
            tombstone=tombstone,
        )

    @staticmethod
    def _response(tag: Tag, usage_count: int) -> TagResponse:
        return TagResponse(
            id=tag.id,
            name=tag.tag_name,
            tag_type=tag.tag_type,
            source=tag.source,
            favorite=bool(tag.favorite),
            usage_count=usage_count,
            revision=int(tag.revision),
            created_at=tag.created_at,
            updated_at=tag.updated_at,
        )

    @staticmethod
    def _normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold().strip()
        return re.sub(r"\s+", " ", value)

    @staticmethod
    def _check_revision(tag: Tag, expected: int) -> None:
        if int(tag.revision) != expected:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "REVISION_CONFLICT",
                    "tag_id": tag.id,
                    "expected_revision": expected,
                    "current_revision": tag.revision,
                },
            )

    @staticmethod
    def _check_file_revision(
        common_file: CommonFile,
        state: MemoryKeeperFileState,
        expected: int,
    ) -> None:
        if int(state.revision or 0) != expected:
            MemoryKeeperFileService._revision_conflict(
                common_file,
                expected,
                int(state.revision or 0),
            )

    @staticmethod
    def _duplicate_name(name: str) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "DUPLICATE_TAG_NAME", "name": name},
        )

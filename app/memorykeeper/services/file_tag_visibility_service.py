from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_tag import CommonFileTag
from app.common.repositories.change_event_repository import (
    ChangeEventRepository,
    ChangeOperation,
)
from app.common.repositories.tag_repository import TagSource, TagType
from app.memorykeeper.models.file_tag_suppression import (
    MemoryKeeperFileTagSuppression,
)
from app.memorykeeper.models.tag import Tag
from app.memorykeeper.schemas.file import FileTagVisibilityMutationResponse
from app.memorykeeper.services.file_service import MemoryKeeperFileService
from app.memorykeeper.services.tag_catalog_service import (
    MemoryKeeperTagCatalogService,
)


class MemoryKeeperFileTagVisibilityService:
    """Mutate one file's projected tag visibility without touching raw AI rows."""

    SERVICE_NAME = "MemoryKeeper"
    RESOURCE_TYPE = "MemoryKeeperFileTag"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.files = MemoryKeeperFileService(db)
        self.catalog = MemoryKeeperTagCatalogService(db)
        self.changes = ChangeEventRepository(db)

    def hide(
        self,
        public_file_id: str,
        identity: str,
        *,
        expected_revision: int,
    ) -> FileTagVisibilityMutationResponse:
        common_file, state = self._locked_file_state(public_file_id, expected_revision)
        entry = self.catalog._require_entry(identity)
        if common_file.id not in entry.file_ids:
            self._file_tag_not_found(public_file_id, identity)

        mutated = self._remove_user_relation(common_file, identity, entry.display_name)
        for canonical in entry.canonical_references:
            self._set_suppression(common_file.id, canonical, hidden=True)
            mutated = True
        if not mutated:
            self._file_tag_not_found(public_file_id, identity)

        state.revision = int(state.revision or 0) + 1
        state.updated_at = datetime.now(timezone.utc)
        self._append_change(
            common_file,
            identity,
            operation=ChangeOperation.DELETE,
            revision=state.revision,
            tombstone=True,
        )
        self.db.commit()
        return FileTagVisibilityMutationResponse(
            file_id=common_file.file_id,
            identity=identity,
            hidden=True,
            revision=state.revision,
        )

    def restore(
        self,
        public_file_id: str,
        identity: str,
        *,
        expected_revision: int,
    ) -> FileTagVisibilityMutationResponse:
        common_file, state = self._locked_file_state(public_file_id, expected_revision)
        entry = self.catalog._require_entry(identity)
        mutated = False

        tag_id = self.catalog._tag_id(identity)
        if tag_id is not None:
            tag = self.catalog.tags.get(tag_id)
            self._ensure_user_relation(common_file, tag)
            mutated = True
        elif identity.startswith("legacy:"):
            mutated = self._restore_legacy_relation(common_file, entry.display_name)

        for canonical in entry.canonical_references:
            if self._set_suppression(common_file.id, canonical, hidden=False):
                mutated = True
        if identity.startswith("ai:") and not entry.canonical_references:
            self._file_tag_not_found(public_file_id, identity)
        if not mutated:
            self._file_tag_not_found(public_file_id, identity)

        state.revision = int(state.revision or 0) + 1
        state.updated_at = datetime.now(timezone.utc)
        self._append_change(
            common_file,
            identity,
            operation=ChangeOperation.UPDATE,
            revision=state.revision,
            tombstone=False,
        )
        self.db.commit()
        return FileTagVisibilityMutationResponse(
            file_id=common_file.file_id,
            identity=identity,
            hidden=False,
            revision=state.revision,
        )

    def _locked_file_state(self, public_file_id: str, expected_revision: int):
        common_file = self.files.require_file(public_file_id, lock=True)
        state = self.files.get_state(common_file, create=True)
        if int(state.revision or 0) != expected_revision:
            MemoryKeeperFileService._revision_conflict(
                common_file,
                expected_revision,
                int(state.revision or 0),
            )
        return common_file, state

    def _remove_user_relation(
        self,
        common_file: CommonFile,
        identity: str,
        display_name: str,
    ) -> bool:
        tag_id = self.catalog._tag_id(identity)
        query = (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.file_id == common_file.id)
            .filter(CommonFileTag.source == TagSource.USER)
            .filter(CommonFileTag.deleted.is_(False))
        )
        if tag_id is not None:
            relation = query.filter(CommonFileTag.memorykeeper_tag_id == tag_id).first()
        elif identity.startswith("legacy:"):
            normalized = self.catalog.curation.normalize(display_name)
            relation = next(
                (
                    item
                    for item in query.filter(
                        CommonFileTag.memorykeeper_tag_id.is_(None)
                    ).all()
                    if self.catalog.curation.normalize(item.tag) == normalized
                ),
                None,
            )
        else:
            relation = None
        if relation is None:
            return False
        relation.deleted = True
        return True

    def _ensure_user_relation(self, common_file: CommonFile, tag: Tag) -> None:
        relation = (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.file_id == common_file.id)
            .filter(CommonFileTag.memorykeeper_tag_id == tag.id)
            .first()
        )
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
            return
        relation.tag = tag.tag_name
        relation.tag_type = TagType.USER
        relation.source = TagSource.USER
        relation.confidence = None
        relation.deleted = False

    def _restore_legacy_relation(self, common_file: CommonFile, display_name: str) -> bool:
        normalized = self.catalog.curation.normalize(display_name)
        for relation in (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.file_id == common_file.id)
            .filter(CommonFileTag.source == TagSource.USER)
            .filter(CommonFileTag.memorykeeper_tag_id.is_(None))
            .all()
        ):
            if self.catalog.curation.normalize(relation.tag) == normalized:
                relation.deleted = False
                return True
        return False

    def _set_suppression(self, file_id: int, canonical: str, *, hidden: bool) -> bool:
        normalized = self.catalog.curation.normalize(canonical)
        item = (
            self.db.query(MemoryKeeperFileTagSuppression)
            .filter(MemoryKeeperFileTagSuppression.file_id == file_id)
            .filter(MemoryKeeperFileTagSuppression.canonical_key == normalized)
            .first()
        )
        if item is None:
            if not hidden:
                return False
            item = MemoryKeeperFileTagSuppression(
                file_id=file_id,
                canonical_key=normalized,
                revision=1,
                deleted=False,
            )
            self.db.add(item)
            self.db.flush()
            return True
        desired_deleted = not hidden
        if bool(item.deleted) == desired_deleted:
            return False
        item.deleted = desired_deleted
        item.revision += 1
        item.updated_at = datetime.now(timezone.utc)
        return True

    def _append_change(
        self,
        common_file: CommonFile,
        identity: str,
        *,
        operation: str,
        revision: int,
        tombstone: bool,
    ) -> None:
        self.changes.append(
            service_name=self.SERVICE_NAME,
            resource_type=self.RESOURCE_TYPE,
            resource_id=f"{common_file.file_id}:{identity}",
            operation=operation,
            revision=revision,
            tombstone=tombstone,
        )

    @staticmethod
    def _file_tag_not_found(public_file_id: str, identity: str) -> None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "FILE_TAG_IDENTITY_NOT_FOUND",
                "file_id": public_file_id,
                "identity": identity,
            },
        )

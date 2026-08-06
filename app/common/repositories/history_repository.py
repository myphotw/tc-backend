"""Metadata history 저장소."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.common.models.metadata_history import CommonMetadataHistory
from app.common.repositories.metadata_priority import MetadataPriority


class HistoryRepository:
    """common_metadata_history 저장소."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create_history(
        self,
        *,
        file_id: int,
        field_name: str,
        old_value: Any,
        new_value: Any,
        source: str,
        priority: MetadataPriority,
        modified_by: str | None = None,
        approved: bool = False,
        commit: bool = True,
    ) -> CommonMetadataHistory:
        """메타데이터 변경 이력을 생성한다."""
        history = CommonMetadataHistory(
            file_id=file_id,
            field_name=field_name,
            old_value=self._stringify(old_value),
            new_value=self._stringify(new_value),
            source=source,
            priority=int(priority),
            modified_by=modified_by,
            approved=approved,
        )
        self.db.add(history)
        if commit:
            self.db.commit()
            self.db.refresh(history)
        return history

    def create_histories(
        self,
        *,
        items: list[dict[str, Any]],
        commit: bool = False,
    ) -> list[CommonMetadataHistory]:
        """변경 이력을 일괄 추가한다 (기본은 flush만, commit은 호출자 정책)."""
        histories: list[CommonMetadataHistory] = []
        for item in items:
            history = CommonMetadataHistory(
                file_id=item["file_id"],
                field_name=item["field_name"],
                old_value=self._stringify(item.get("old_value")),
                new_value=self._stringify(item.get("new_value")),
                source=item["source"],
                priority=int(item["priority"]),
                modified_by=item.get("modified_by"),
                approved=bool(item.get("approved", False)),
            )
            histories.append(history)
        if histories:
            self.db.add_all(histories)
            if commit:
                self.db.commit()
        return histories

    def get_latest_priority(self, *, file_id: int, field_name: str) -> int:
        """특정 필드의 마지막 저장 priority를 조회한다."""
        priorities = self.get_latest_priorities(
            file_id=file_id,
            field_names=[field_name],
        )
        return priorities.get(field_name, 0)

    def get_latest_priorities(
        self,
        *,
        file_id: int,
        field_names: list[str] | None = None,
    ) -> dict[str, int]:
        """필드별 최신 priority를 한 번에 조회한다."""
        query = self.db.query(
            CommonMetadataHistory.field_name,
            func.max(CommonMetadataHistory.id).label("max_id"),
        ).filter(CommonMetadataHistory.file_id == file_id)
        if field_names is not None:
            if not field_names:
                return {}
            query = query.filter(CommonMetadataHistory.field_name.in_(field_names))
        subq = query.group_by(CommonMetadataHistory.field_name).subquery()
        rows = (
            self.db.query(
                CommonMetadataHistory.field_name,
                CommonMetadataHistory.priority,
            )
            .join(subq, CommonMetadataHistory.id == subq.c.max_id)
            .all()
        )
        return {field_name: int(priority) for field_name, priority in rows}

    def get_history(
        self,
        *,
        file_id: int,
        field_name: str | None = None,
    ) -> list[CommonMetadataHistory]:
        """파일 메타데이터 변경 이력을 조회한다."""
        query = self.db.query(CommonMetadataHistory).filter(
            CommonMetadataHistory.file_id == file_id
        )
        if field_name is not None:
            query = query.filter(CommonMetadataHistory.field_name == field_name)
        return query.order_by(CommonMetadataHistory.created_at.desc()).all()

    def _stringify(self, value: Any) -> str | None:
        """이력 저장을 위해 값을 문자열로 변환한다."""
        if value is None:
            return None
        return str(value)

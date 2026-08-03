from __future__ import annotations

from typing import Any

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

    def get_latest_priority(self, *, file_id: int, field_name: str) -> int:
        """특정 필드의 마지막 저장 priority를 조회한다."""
        history = (
            self.db.query(CommonMetadataHistory)
            .filter(CommonMetadataHistory.file_id == file_id)
            .filter(CommonMetadataHistory.field_name == field_name)
            .order_by(CommonMetadataHistory.created_at.desc(), CommonMetadataHistory.id.desc())
            .first()
        )
        if history is None:
            return 0
        return history.priority

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

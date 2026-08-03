from __future__ import annotations

from sqlalchemy.orm import Session

from app.common.models.file_tag import CommonFileTag


class TagType:
    """태그 타입 값."""

    AI = "AI"
    ASTRO = "ASTRO"
    USER = "USER"
    SYSTEM = "SYSTEM"


class TagSource:
    """태그 출처 값. 승인(approved) 없이 AI / USER만 사용한다."""

    AI = "AI"
    USER = "USER"


class TagRepository:
    """common_file_tags 저장소."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def save_ai_tag(
        self,
        *,
        file_id: int,
        tag: str,
        confidence: float | None = None,
        tag_type: str = TagType.AI,
    ) -> CommonFileTag | None:
        """
        AI Tag를 저장한다.

        USER Tag가 동일 의미로 존재하면 생성하지 않는다.
        기존 AI Tag가 있으면 confidence만 갱신한다.
        """
        normalized = self._normalize_tag(tag)
        if not normalized:
            raise ValueError("tag is required")
        if confidence is not None and not 0 <= confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")
        if self.exists_user_tag(file_id=file_id, tag=normalized):
            return None

        existing = self._find_active_tag(
            file_id=file_id,
            tag=normalized,
            source=TagSource.AI,
        )
        if existing is not None:
            existing.confidence = confidence
            existing.tag_type = tag_type
            self.db.commit()
            self.db.refresh(existing)
            return existing

        item = CommonFileTag(
            file_id=file_id,
            tag=normalized,
            tag_type=tag_type,
            source=TagSource.AI,
            confidence=confidence,
            deleted=False,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def save_user_tag(
        self,
        *,
        file_id: int,
        tag: str,
        replaces: str | None = None,
    ) -> CommonFileTag:
        """
        USER Tag를 저장한다.

        기존 AI Tag는 soft delete 후 source=USER Tag를 생성한다.
        confidence는 NULL이다.
        """
        normalized = self._normalize_tag(tag)
        if not normalized:
            raise ValueError("tag is required")

        if replaces:
            self.remove_ai_tag(file_id=file_id, tag=replaces)
        self.remove_ai_tag(file_id=file_id, tag=normalized)

        existing = self._find_active_tag(
            file_id=file_id,
            tag=normalized,
            source=TagSource.USER,
        )
        if existing is not None:
            existing.confidence = None
            existing.tag_type = TagType.USER
            self.db.commit()
            self.db.refresh(existing)
            return existing

        item = CommonFileTag(
            file_id=file_id,
            tag=normalized,
            tag_type=TagType.USER,
            source=TagSource.USER,
            confidence=None,
            deleted=False,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def exists_user_tag(self, *, file_id: int, tag: str) -> bool:
        """동일 의미의 활성 USER Tag 존재 여부를 반환한다."""
        return (
            self._find_active_tag(
                file_id=file_id,
                tag=tag,
                source=TagSource.USER,
            )
            is not None
        )

    def remove_ai_tag(self, *, file_id: int, tag: str) -> bool:
        """동일 의미의 활성 AI Tag를 soft delete한다."""
        item = self._find_active_tag(
            file_id=file_id,
            tag=tag,
            source=TagSource.AI,
        )
        if item is None:
            return False
        item.deleted = True
        self.db.commit()
        return True

    def get_tags(
        self,
        *,
        file_id: int,
        tag_type: str | None = None,
        source: str | None = None,
        include_deleted: bool = False,
    ) -> list[CommonFileTag]:
        """파일 태그 목록을 조회한다. 기본은 삭제되지 않은 Tag만 반환한다."""
        query = self.db.query(CommonFileTag).filter(CommonFileTag.file_id == file_id)
        if not include_deleted:
            query = query.filter(CommonFileTag.deleted.is_(False))
        if tag_type is not None:
            query = query.filter(CommonFileTag.tag_type == tag_type)
        if source is not None:
            query = query.filter(CommonFileTag.source == source)
        return query.order_by(CommonFileTag.created_at.desc()).all()

    def delete_tag(self, *, tag_id: int) -> bool:
        """태그를 soft delete한다."""
        tag = self.db.query(CommonFileTag).filter(CommonFileTag.id == tag_id).first()
        if tag is None:
            return False
        tag.deleted = True
        self.db.commit()
        return True

    def _find_active_tag(
        self,
        *,
        file_id: int,
        tag: str,
        source: str,
    ) -> CommonFileTag | None:
        """동일 의미의 활성 Tag를 찾는다."""
        normalized = self._normalize_tag(tag)
        if not normalized:
            return None

        candidates = (
            self.db.query(CommonFileTag)
            .filter(
                CommonFileTag.file_id == file_id,
                CommonFileTag.source == source,
                CommonFileTag.deleted.is_(False),
            )
            .all()
        )
        for item in candidates:
            if self._normalize_tag(item.tag) == normalized:
                return item
        return None

    @staticmethod
    def _normalize_tag(tag: str) -> str:
        """동일 의미 비교를 위한 Tag 정규화."""
        return " ".join(str(tag).strip().split())

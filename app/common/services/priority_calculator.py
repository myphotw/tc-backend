"""Vision Queue Priority 계산기."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class PriorityCalculator:
    """
    Vision Queue Priority를 계산한다.

    현재 정책은 함수로 구현하며, 향후 정책 변경이 가능하도록 분리한다.
    """

    TODAY_BONUS = 50
    RECENT_30_DAYS_BONUS = 30
    NO_GPS_BONUS = 20
    FAVORITE_BONUS = 30
    MAX_PRIORITY = 100
    MIN_PRIORITY = 0

    def calculate(
        self,
        *,
        uploaded_at: datetime | None = None,
        has_gps: bool = True,
        is_favorite: bool = False,
        vision_completed: bool = False,
        now: datetime | None = None,
    ) -> int | None:
        """
        Vision Queue Priority를 계산한다.

        Args:
            uploaded_at: 파일 업로드/생성 시각
            has_gps: GPS 존재 여부
            is_favorite: 사용자 즐겨찾기 여부
            vision_completed: 이미 Vision 완료 여부
            now: 기준 시각

        Returns:
            int | None: 0~100 priority. Vision 완료면 Queue 생성 안 함(None)
        """
        if vision_completed:
            return None

        current = now or datetime.now(timezone.utc)
        priority = 0

        if uploaded_at is not None:
            uploaded_at_utc = self._ensure_utc(uploaded_at)
            current_utc = self._ensure_utc(current)

            if self._is_same_day(uploaded_at_utc, current_utc):
                priority += self.TODAY_BONUS
            elif uploaded_at_utc >= current_utc - timedelta(days=30):
                priority += self.RECENT_30_DAYS_BONUS

        if not has_gps:
            priority += self.NO_GPS_BONUS

        if is_favorite:
            priority += self.FAVORITE_BONUS

        return max(self.MIN_PRIORITY, min(self.MAX_PRIORITY, priority))

    def _ensure_utc(self, value: datetime) -> datetime:
        """timezone-aware UTC datetime으로 정규화한다."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _is_same_day(self, left: datetime, right: datetime) -> bool:
        """같은 UTC 날짜인지 확인한다."""
        left_utc = self._ensure_utc(left)
        right_utc = self._ensure_utc(right)
        return left_utc.date() == right_utc.date()

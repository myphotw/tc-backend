"""성능 계측 유틸 (진단 전용, 응답 구조 변경 없음)."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine

perf_logger = logging.getLogger("tc.perf")


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def log_perf(event_name: str, **fields: Any) -> None:
    """구조화 성능 로그. 개인정보/SQL 본문은 남기지 않는다."""
    parts = [f"event={event_name}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    perf_logger.info(" ".join(parts))


@dataclass
class Stopwatch:
    """구간 타이머."""

    marks: dict[str, float] = field(default_factory=dict)
    _starts: dict[str, float] = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)

    def start(self, name: str) -> None:
        self._starts[name] = time.perf_counter()

    def stop(self, name: str) -> float:
        started = self._starts.pop(name, None)
        if started is None:
            return 0.0
        value = elapsed_ms(started)
        self.marks[name] = value
        return value

    def total_ms(self) -> float:
        return elapsed_ms(self.started_at)


class QueryCounter:
    """SQLAlchemy 실행 횟수 카운터 (SQL 본문 미기록)."""

    def __init__(self, bind: Engine) -> None:
        self.bind = bind
        self.count = 0

    def _before_cursor_execute(
        self,
        conn: Any,
        cursor: Any,
        statement: Any,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        self.count += 1

    def __enter__(self) -> "QueryCounter":
        event.listen(self.bind, "before_cursor_execute", self._before_cursor_execute)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        event.remove(self.bind, "before_cursor_execute", self._before_cursor_execute)


@contextmanager
def measure(name: str, **fields: Any) -> Iterator[Stopwatch]:
    """with 블록 전체 시간을 기록한다."""
    watch = Stopwatch()
    try:
        yield watch
    finally:
        log_perf(name, elapsed_ms=watch.total_ms(), **fields, **watch.marks)

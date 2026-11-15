"""Cooperative shutdown: handlers only notify; workers clean up at safe points."""

from __future__ import annotations

import signal
from threading import Event
from time import monotonic


class WorkerShutdownRequested(Exception):
    """Internal control flow, never a provider/job failure."""


class WorkerShutdown:
    def __init__(self) -> None:
        self._event = Event()
        self._requested = False
        self._previous_handlers = {}

    @property
    def requested(self) -> bool:
        return self._requested

    def request(self, signum=None, frame=None) -> None:
        # A real signal handler only assigns a flag: Event.set() takes a lock
        # and must not re-enter an interrupted Event.wait()/set() critical section.
        self._requested = True
        if signum is None:
            self._event.set()  # Ordinary callers may wake the waiter directly.

    def wait(self, seconds: float) -> bool:
        # Windows Python may defer Python handlers until a lock wait returns.
        # Bound each wait so real signals also wake quota/idle waits promptly.
        deadline = monotonic() + max(0, seconds)
        while not self.requested:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            self._event.wait(min(remaining, 0.1))
        return True

    def check(self) -> None:
        if self.requested:
            raise WorkerShutdownRequested()

    def __enter__(self) -> WorkerShutdown:
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                self._previous_handlers[signum] = signal.signal(signum, self.request)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        for signum, handler in self._previous_handlers.items():
            signal.signal(signum, handler)
        self._previous_handlers.clear()

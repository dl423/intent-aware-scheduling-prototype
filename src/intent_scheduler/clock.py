"""Clock abstractions used by the scheduler and compressed-time demo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Protocol


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("clock datetimes must be timezone-aware")
    return value.astimezone(timezone.utc)


class Clock(Protocol):
    """Minimal clock interface consumed by deterministic components."""

    def now(self) -> datetime:
        """Return the current, timezone-aware instant."""


class SystemClock:
    """Wall clock implementation for the service."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# A more discoverable alias for callers that prefer the term used in the README.
RealClock = SystemClock


class SimulatedClock:
    """Manually advanced clock for deterministic tests and compressed demos."""

    def __init__(self, start: datetime | None = None) -> None:
        self._current = _as_utc(start or datetime(2026, 1, 1, tzinfo=timezone.utc))
        self._lock = RLock()

    def now(self) -> datetime:
        with self._lock:
            return self._current

    def advance(
        self,
        amount: timedelta | float = 0,
        *,
        seconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
    ) -> datetime:
        """Advance time and return the new instant.

        ``amount`` accepts a timedelta or a number of seconds. The keyword form
        is convenient for demos. Negative movement is rejected because it can
        invalidate queue-wait and deadline accounting.
        """

        if amount != 0 and any((seconds, minutes, hours)):
            raise ValueError("pass either amount or keyword units, not both")
        if any((seconds, minutes, hours)):
            delta = timedelta(seconds=seconds, minutes=minutes, hours=hours)
        else:
            delta = amount if isinstance(amount, timedelta) else timedelta(seconds=amount)
        if delta < timedelta(0):
            raise ValueError("simulated time cannot move backwards")
        with self._lock:
            self._current += delta
            return self._current

    def set(self, instant: datetime) -> datetime:
        """Move to a later absolute instant and return it."""

        instant = _as_utc(instant)
        with self._lock:
            if instant < self._current:
                raise ValueError("simulated time cannot move backwards")
            self._current = instant
            return self._current

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ResetEvent:
    event_id: str
    announced_at: datetime
    summary: str
    url: str


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    used_percent: float | None
    reset_at: datetime | None
    window_minutes: int | None
    banked_resets: int | None
    banked_reset_expirations: tuple[datetime, ...] = ()
    checked_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class AppState:
    last_seen_reset_id: str | None = None
    last_global_reset: ResetEvent | None = None
    last_known_usage: UsageSnapshot | None = None

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
    effective_at: datetime | None = None
    confirmed: bool = True


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    used_percent: float | None
    reset_at: datetime | None
    window_minutes: int | None
    banked_resets: int | None
    banked_reset_expirations: tuple[datetime, ...] = ()
    checked_at: datetime = field(default_factory=utc_now)
    five_hour_used_percent: float | None = None
    five_hour_reset_at: datetime | None = None
    five_hour_window_minutes: int | None = None


@dataclass(slots=True)
class AppState:
    keep_five_hour_window_active: bool = False
    last_seen_reset_id: str | None = None
    last_global_reset: ResetEvent | None = None
    last_known_usage: UsageSnapshot | None = None

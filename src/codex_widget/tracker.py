from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import ResetEvent

DEFAULT_FEED_URL = "https://codex-reset.com/api/feed"
_MAX_RESPONSE_BYTES = 1_000_000


class TrackerError(RuntimeError):
    pass


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TrackerError("reset event has no timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrackerError(f"invalid reset timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_confirmed_reset(event: object) -> bool:
    if (
        not isinstance(event, dict)
        or event.get("group") != "reset"
        or event.get("preview") is not False
    ):
        return False
    if event.get("reset_verification_status") == "confirmed":
        return True
    return event.get("observation_result") == "reset_observed"


class TrackerClient:
    def __init__(
        self,
        endpoint: str = DEFAULT_FEED_URL,
        *,
        timeout: float = 10.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._opener = opener

    def fetch_latest_confirmed(self) -> ResetEvent:
        request = Request(
            self.endpoint,
            headers={
                "Accept": "application/json",
                "User-Agent": "codex-widget/0.1",
            },
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise TrackerError(f"reset feed request failed: {exc}") from exc
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise TrackerError("reset feed response is unexpectedly large")
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TrackerError("reset feed returned invalid JSON") from exc
        if not isinstance(document, dict) or not isinstance(document.get("events"), list):
            raise TrackerError("reset feed has an unexpected shape")
        if document.get("stale") is True:
            raise TrackerError("reset feed reports stale data")

        confirmed: list[tuple[datetime, dict[str, Any]]] = []
        for item in document["events"]:
            if not _is_confirmed_reset(item):
                continue
            try:
                announced_at = _parse_timestamp(item.get("announced_at"))
            except TrackerError:
                continue
            confirmed.append((announced_at, item))
        if not confirmed:
            raise TrackerError("reset feed contains no confirmed reset")

        announced_at, latest = max(confirmed, key=lambda pair: pair[0])
        event_id = latest.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise TrackerError("confirmed reset has no stable ID")
        tweets = document.get("tweets")
        full_text = None
        if isinstance(tweets, list):
            full_text = next(
                (
                    tweet.get("text")
                    for tweet in tweets
                    if isinstance(tweet, dict)
                    and tweet.get("id") == event_id
                    and isinstance(tweet.get("text"), str)
                ),
                None,
            )
        return ResetEvent(
            event_id=event_id,
            announced_at=announced_at,
            summary=full_text
            or str(latest.get("summary") or "Global Codex reset announced."),
            url=str(latest.get("url") or ""),
        )

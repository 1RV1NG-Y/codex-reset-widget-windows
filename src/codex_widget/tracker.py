from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import ResetEvent

PRIMARY_FEED_URL = "https://codex-reset.com/api/feed"
FALLBACK_FEED_URL = "https://codex-resets.com/api/v1/resets"
DEFAULT_FEED_URLS = (PRIMARY_FEED_URL, FALLBACK_FEED_URL)
_MAX_RESPONSE_BYTES = 1_000_000


class TrackerError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)




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
    if (
        event.get("reset_verification_status") == "confirmed"
        or event.get("observation_result") == "reset_observed"
    ):
        return True
    return (
        event.get("source") == "archive"
        and event.get("source_label") == "Verified archive"
    )


def _is_active_pending_reset(document: dict[str, Any], event: object) -> bool:
    if (
        not isinstance(event, dict)
        or event.get("group") != "reset"
        or event.get("preview") is not True
        or event.get("reset_verification_status") != "pending"
    ):
        return False
    signal = document.get("signal")
    return (
        isinstance(signal, dict)
        and signal.get("active") is True
        and signal.get("tweet_id") == event.get("id")
    )


class TrackerClient:
    def __init__(
        self,
        endpoint: str | None = None,
        *,
        timeout: float = 10.0,
        opener: Callable[..., Any] = urlopen,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.endpoints = (endpoint,) if endpoint is not None else DEFAULT_FEED_URLS
        self.timeout = timeout
        self._opener = opener
        self._now = now

    def fetch_latest_relevant(self) -> ResetEvent:
        errors: list[str] = []
        for endpoint in self.endpoints:
            try:
                document = self._fetch_document(endpoint)
                return self._parse_document(document)
            except TrackerError as exc:
                errors.append(f"{endpoint}: {exc}")
        detail = "; ".join(errors) or "no reset sources configured"
        raise TrackerError(f"all reset sources failed: {detail}")

    def _fetch_document(self, endpoint: str) -> dict[str, Any]:
        request = Request(
            endpoint,
            headers={
                "Accept": "application/json",
                "User-Agent": "codex-widget/0.1",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise TrackerError(f"request failed: {exc}") from exc
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise TrackerError("response is unexpectedly large")
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TrackerError("returned invalid JSON") from exc
        if not isinstance(document, dict) or not (
            isinstance(document.get("events"), list)
            or isinstance(document.get("data"), list)
        ):
            raise TrackerError("has an unexpected shape")
        if document.get("stale") is True:
            raise TrackerError("reports stale data")
        return document

    def _parse_document(self, document: dict[str, Any]) -> ResetEvent:
        fallback = document.get("data")
        if isinstance(fallback, list):
            meta = document.get("meta")
            if not isinstance(meta, dict) or meta.get("api_version") != "v1":
                raise TrackerError("fallback has an unsupported API version")
            generated_at = _parse_timestamp(meta.get("generated_at"))
            age = self._now().astimezone(UTC) - generated_at
            if age < -timedelta(minutes=5) or age > timedelta(hours=24):
                raise TrackerError("fallback reports stale data")
            return self._parse_fallback_events(fallback)
        events = document["events"]
        if any(isinstance(item, dict) and "tweet_id" in item for item in events):
            return self._parse_fallback_events(events)
        return self._parse_primary_feed(document)

    @staticmethod
    def _parse_primary_feed(document: dict[str, Any]) -> ResetEvent:
        relevant: list[tuple[datetime, dict[str, Any], bool]] = []
        for item in document["events"]:
            confirmed = _is_confirmed_reset(item)
            if not confirmed and not _is_active_pending_reset(document, item):
                continue
            try:
                announced_at = _parse_timestamp(item.get("announced_at"))
            except TrackerError:
                continue
            relevant.append((announced_at, item, confirmed))
        if not relevant:
            raise TrackerError("contains no relevant reset")

        announced_at, latest, confirmed = max(
            relevant,
            key=lambda candidate: candidate[0],
        )
        event_id = latest.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise TrackerError("reset event has no stable ID")
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
        effective_at = None
        if latest.get("effective_at") is not None:
            try:
                effective_at = _parse_timestamp(latest.get("effective_at"))
            except TrackerError:
                pass
        return ResetEvent(
            event_id=event_id,
            announced_at=announced_at,
            summary=full_text
            or str(latest.get("summary") or "Global Codex reset announced."),
            url=str(latest.get("url") or ""),
            effective_at=effective_at,
            confirmed=confirmed,
        )

    @staticmethod
    def _parse_fallback_events(events: list[object]) -> ResetEvent:
        regular: list[tuple[datetime, dict[str, Any]]] = []
        for item in events:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            if (
                item.get("reset_type") != "regular"
                or not isinstance(source, dict)
                or source.get("type") != "x_post"
                or source.get("author") != "thsottiaux"
            ):
                continue
            try:
                announced_at = _parse_timestamp(item.get("announced_at"))
            except TrackerError:
                continue
            regular.append((announced_at, item))
        if not regular:
            raise TrackerError("contains no regular reset")

        announced_at, latest = max(regular, key=lambda candidate: candidate[0])
        event_id = latest.get("id") or latest.get("tweet_id")
        if not isinstance(event_id, str) or not event_id:
            raise TrackerError("reset event has no stable ID")
        return ResetEvent(
            event_id=event_id,
            announced_at=announced_at,
            summary=str(latest.get("text") or "Global Codex reset announced."),
            url=str(
                latest.get("tweet_url")
                or (
                    latest.get("source", {}).get("url")
                    if isinstance(latest.get("source"), dict)
                    else ""
                )
                or ""
            ),
        )

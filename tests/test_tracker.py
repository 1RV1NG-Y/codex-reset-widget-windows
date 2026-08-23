from __future__ import annotations

import io
import json
import unittest
from datetime import UTC, datetime
from urllib.error import URLError
from codex_widget.tracker import FALLBACK_FEED_URL, TrackerClient, TrackerError


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def opener_for(document):
    def open_request(_request, *, timeout):
        assert timeout == 10
        return Response(json.dumps(document).encode())

    return open_request


class TrackerClientTests(unittest.TestCase):
    def test_selects_latest_confirmed_reset_and_full_tweet_text(self):
        document = {
            "stale": False,
            "events": [
                {
                    "id": "hint",
                    "group": "signal",
                    "type": "reset",
                    "announced_at": "2026-08-10T09:00:00Z",
                },
                {
                    "id": "new",
                    "group": "reset",
                    "type": "reset",
                    "preview": False,
                    "observation_result": "reset_observed",
                    "announced_at": "2026-08-10T08:00:00Z",
                    "summary": "truncated",
                    "url": "https://example.test/new",
                },
                {
                    "id": "old",
                    "group": "reset",
                    "type": "reset",
                    "preview": False,
                    "reset_verification_status": "confirmed",
                    "announced_at": "2026-08-01T08:00:00Z",
                },
            ],
            "tweets": [{"id": "new", "text": "The complete reset announcement"}],
        }

        event = TrackerClient(opener=opener_for(document)).fetch_latest_relevant()

        self.assertEqual(event.event_id, "new")
        self.assertEqual(event.summary, "The complete reset announcement")
        self.assertEqual(event.url, "https://example.test/new")

    def test_selects_active_pending_reset_for_local_confirmation(self):
        document = {
            "stale": False,
            "signal": {
                "tweet_id": "pending",
                "active": True,
            },
            "events": [
                {
                    "id": "pending",
                    "group": "reset",
                    "preview": True,
                    "reset_verification_status": "pending",
                    "announced_at": "2026-08-10T20:00:00Z",
                    "summary": "Reset planned for Monday",
                },
                {
                    "id": "confirmed",
                    "group": "reset",
                    "preview": False,
                    "reset_verification_status": "confirmed",
                    "announced_at": "2026-08-08T20:00:00Z",
                },
            ],
        }

        event = TrackerClient(opener=opener_for(document)).fetch_latest_relevant()

        self.assertEqual(event.event_id, "pending")
        self.assertFalse(event.confirmed)

    def test_accepts_verified_archive_event_without_live_status_fields(self):
        document = {
            "stale": False,
            "events": [
                {
                    "id": "archive",
                    "group": "reset",
                    "preview": False,
                    "source": "archive",
                    "source_label": "Verified archive",
                    "announced_at": "2026-08-11T00:27:44Z",
                }
            ],
        }

        event = TrackerClient(opener=opener_for(document)).fetch_latest_relevant()

        self.assertEqual(event.event_id, "archive")
        self.assertTrue(event.confirmed)

    def test_falls_back_and_ignores_banked_resets(self):
        fallback = {
            "data": [
                {
                    "id": "banked",
                    "text": "Banked reset",
                    "announced_at": "2026-08-21T23:40:12Z",
                    "reset_type": "banked",
                    "source": {
                        "type": "x_post",
                        "author": "thsottiaux",
                        "url": "https://example.test/banked",
                    },
                },
                {
                    "id": "regular",
                    "text": "Regular reset",
                    "announced_at": "2026-08-13T01:01:37Z",
                    "reset_type": "regular",
                    "source": {
                        "type": "x_post",
                        "author": "thsottiaux",
                        "url": "https://example.test/regular",
                    },
                },
            ],
            "meta": {
                "api_version": "v1",
                "generated_at": "2026-08-23T20:04:25Z",
            },
        }

        def open_request(request, *, timeout):
            self.assertEqual(timeout, 10)
            if request.full_url == "https://codex-reset.com/api/feed":
                raise URLError("primary unavailable")
            return Response(json.dumps(fallback).encode())

        event = TrackerClient(
            opener=open_request,
            now=lambda: datetime(2026, 8, 23, 20, 5, tzinfo=UTC),
        ).fetch_latest_relevant()
        self.assertEqual(event.event_id, "regular")
        self.assertEqual(event.summary, "Regular reset")
        self.assertEqual(event.url, "https://example.test/regular")

    def test_rejects_stale_versioned_fallback(self):
        document = {
            "data": [],
            "meta": {
                "api_version": "v1",
                "generated_at": "2026-08-20T20:00:00Z",
            },
        }
        client = TrackerClient(
            endpoint=FALLBACK_FEED_URL,
            opener=opener_for(document),
            now=lambda: datetime(2026, 8, 23, 20, tzinfo=UTC),
        )

        with self.assertRaisesRegex(TrackerError, "stale"):
            client.fetch_latest_relevant()

    def test_rejects_feed_that_reports_stale_data(self):
        client = TrackerClient(opener=opener_for({"stale": True, "events": []}))

        with self.assertRaisesRegex(TrackerError, "stale"):
            client.fetch_latest_relevant()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import unittest

from codex_widget.tracker import TrackerClient, TrackerError


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

        event = TrackerClient(opener=opener_for(document)).fetch_latest_confirmed()

        self.assertEqual(event.event_id, "new")
        self.assertEqual(event.summary, "The complete reset announcement")
        self.assertEqual(event.url, "https://example.test/new")

    def test_rejects_feed_that_reports_stale_data(self):
        client = TrackerClient(opener=opener_for({"stale": True, "events": []}))

        with self.assertRaisesRegex(TrackerError, "stale"):
            client.fetch_latest_confirmed()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from codex_widget.cli import _prefer_x11_backend


class BackendSelectionTests(unittest.TestCase):
    def test_x11_is_preferred_when_xwayland_is_available(self):
        with patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True):
            _prefer_x11_backend()
            self.assertEqual(os.environ["GDK_BACKEND"], "x11")

    def test_explicit_backend_is_preserved(self):
        with patch.dict(
            os.environ,
            {"DISPLAY": ":0", "GDK_BACKEND": "wayland"},
            clear=True,
        ):
            _prefer_x11_backend()
            self.assertEqual(os.environ["GDK_BACKEND"], "wayland")

    def test_backend_is_unchanged_without_xwayland(self):
        with patch.dict(os.environ, {}, clear=True):
            _prefer_x11_backend()
            self.assertNotIn("GDK_BACKEND", os.environ)


if __name__ == "__main__":
    unittest.main()

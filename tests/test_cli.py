from __future__ import annotations

import os
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_widget.cli import _prefer_x11_backend, main


class WindowsDispatchTests(unittest.TestCase):
    def test_windows_dispatches_arguments_without_importing_gtk(self):
        application = Mock()
        application.run.return_value = 0
        module = SimpleNamespace(CodexWidgetApplication=Mock(return_value=application))
        with patch('codex_widget.cli.sys.platform', 'win32'), patch.dict(
            sys.modules, {'codex_widget.windows_app': module}
        ), patch('codex_widget.cli._prefer_x11_backend') as backend:
            self.assertEqual(main(['--daemon']), 0)
        application.run.assert_called_once_with([sys.argv[0], '--daemon'])
        backend.assert_not_called()


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

import os
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.log = Path(self.directory.name) / 'CodexWidget' / 'launcher.log'
        self.run_launcher = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / 'launch-codex-widget.pyw')
        )['run']

    def launch(self, main):
        with patch.dict(os.environ, {'LOCALAPPDATA': self.directory.name}), patch.object(
            sys, 'argv', ['launch-codex-widget.pyw', '--daemon']
        ), patch.dict(sys.modules, {'codex_widget.cli': SimpleNamespace(main=main)}):
            return self.run_launcher()

    def test_successful_launches_append_start_and_exit_records(self):
        for _ in range(2):
            self.assertEqual(self.launch(Mock(return_value=0)), 0)
        text = self.log.read_text(encoding='utf-8')
        self.assertEqual(text.count('Starting pid='), 2)
        self.assertEqual(text.count('Exiting pid='), 2)

    def test_daemon_failure_returns_nonzero_and_records_traceback(self):
        dialog = Mock()
        fake_ctypes = SimpleNamespace(windll=SimpleNamespace(user32=SimpleNamespace(MessageBoxW=dialog)))
        with patch.dict(sys.modules, {'ctypes': fake_ctypes}):
            self.assertEqual(self.launch(Mock(side_effect=RuntimeError('launch broke'))), 1)
        dialog.assert_not_called()
        self.assertIn('RuntimeError: launch broke', self.log.read_text(encoding='utf-8'))

    def test_import_failure_is_logged(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': self.directory.name}), patch.object(
            sys, 'argv', ['launch-codex-widget.pyw', '--daemon']
        ), patch.dict(sys.modules, {'codex_widget.cli': None}):
            self.assertEqual(self.run_launcher(), 1)
        self.assertIn('ModuleNotFoundError', self.log.read_text(encoding='utf-8'))

    def test_explicit_exit_status_is_preserved(self):
        self.assertEqual(self.launch(Mock(side_effect=SystemExit(2))), 2)
        self.assertIn('code=2', self.log.read_text(encoding='utf-8'))

    def test_installed_launcher_uses_same_physical_state_and_endpoint(self):
        from codex_widget.state import _default_state_path
        from codex_widget.windows_app import _user_data_dir

        installation = Path(self.directory.name) / 'physical installation'
        installation.mkdir()
        (installation / 'installed.marker').touch()
        source = Path(__file__).resolve().parents[1] / 'launch-codex-widget.pyw'
        launcher = installation / source.name
        launcher.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
        run = runpy.run_path(str(launcher))['run']

        def main():
            self.assertEqual(_default_state_path(), installation / 'state.json')
            self.assertEqual(_user_data_dir(), installation)
            return 0

        with patch.dict(os.environ, {'LOCALAPPDATA': str(Path(self.directory.name) / 'different profile view')}, clear=True), patch.object(
            sys, 'argv', [str(launcher), '--daemon']
        ), patch.dict(sys.modules, {'codex_widget.cli': SimpleNamespace(main=main)}):
            self.assertEqual(run(), 0)
        self.assertTrue((installation / 'launcher.log').is_file())

"""Offline entry-point checks, also executed inside the actual producer image."""
import pathlib
import runpy
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

class Packaging(unittest.TestCase):
    def test_collector_module_dispatches_to_existing_main(self):
        with patch('producers.live_market.main') as main:
            runpy.run_module('producers.live_market', run_name='__main__')
        main.assert_called_once_with()

    def test_tradier_launcher_resolves_existing_child(self):
        child = MagicMock()
        child.wait.return_value = 0
        with patch('subprocess.Popen', return_value=child) as start:
            with self.assertRaises(SystemExit) as exit:
                runpy.run_module('producers.tradier.launcher', run_name='__main__')
        self.assertEqual(exit.exception.code, 0)
        command = start.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, '-u'])
        self.assertEqual(command[2:], ['-m', 'producers.tradier'])

    def test_scanner_supervisor_uses_runnable_module(self):
        from producers.options_scanner import cli, Config
        real_run = subprocess.run
        commands = []
        def child(command, **kwargs):
            commands.append(command)
            # Launch the actual child module in a separate interpreter, using
            # its help branch to keep this packaging check entirely offline.
            return real_run(command[:4] + ['--help'], check=True,
                            capture_output=True, text=True, cwd=ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            day = pathlib.Path(tmp) / '2026-10-02'
            with patch.object(cli.Config, 'from_env', return_value=Config(upload=False)), \
                 patch.object(cli, 'write_active_attempt'), \
                 patch.object(cli.subprocess, 'run', side_effect=child):
                result = cli.main(['snapshot-supervise', '--dir', str(day),
                                   '--sweep', '1', '--open-ms', '1', '--close-ms', '2'])
        self.assertEqual(result, 0)
        self.assertEqual(commands[0][:4], [sys.executable, '-m',
                         'producers.options_scanner', 'snapshot'])

if __name__ == '__main__':
    unittest.main()

"""Offline entry-point checks, also executed inside the actual producer image."""
import pathlib
import runpy
import sys
import tomllib
import unittest
from unittest.mock import patch, MagicMock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

class Packaging(unittest.TestCase):
    def test_collector_module_dispatches_to_existing_main(self):
        with patch('collector.main') as main:
            runpy.run_module('collector', run_name='__main__')
        main.assert_called_once_with()

    def test_tradier_launcher_resolves_existing_child(self):
        child = MagicMock()
        child.wait.return_value = 0
        with patch('subprocess.Popen', return_value=child) as start:
            with self.assertRaises(SystemExit) as exit:
                runpy.run_module('collectors.tradier.launcher', run_name='__main__')
        self.assertEqual(exit.exception.code, 0)
        command = start.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, '-u'])
        self.assertEqual(pathlib.Path(command[2]), ROOT / 'scripts/moo144_tradier_collector.py')
        self.assertTrue(pathlib.Path(command[2]).is_file())

    def test_service_manifests_share_existing_image(self):
        for component in ['collector', 'collectors/tradier', 'scanners/oa203']:
            config = tomllib.loads((ROOT / component / 'railway.toml').read_text())
            self.assertTrue((ROOT / config['build']['dockerfilePath']).is_file())
            self.assertEqual(config['deploy']['restartPolicyType'].lower(), 'never')
            self.assertNotIn('cronSchedule', config['deploy'])

if __name__ == '__main__':
    unittest.main()

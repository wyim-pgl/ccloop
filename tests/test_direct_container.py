"""Container transport must never enter a scheduler or force client approvals."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/direct_container.py'


class DirectContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.project = self.root / 'project with spaces'
        self.project.mkdir()
        self.code_home = self.root / 'private code home'
        self.code_home.mkdir()
        self.singularity = self.root / 'singularity'
        self.singularity.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
            'print(json.dumps({"argv":sys.argv[1:],"role":os.environ.get("CCLOOP_REVIEWER")}))\n')
        self.singularity.chmod(0o700)
        self.image = self.root / 'image.sif'
        self.image.touch()
        self.profile = self.root / 'profile.json'
        self.settings = {'singularity': str(self.singularity), 'image': str(self.image),
                         'executable': str(self.home / '.kimi-code/bin/kimi'),
                         'binds': ['/cm:/cm'], 'environment': {'PATH': '/usr/bin:/bin'}}
        self.env = dict(os.environ, HOME=str(self.home), KIMI_CODE_HOME=str(self.code_home),
                        CCLOOP_REVIEWER='1')

    def call(self, *arguments):
        self.profile.write_text(json.dumps(self.settings))
        return subprocess.run([sys.executable, str(SCRIPT), '--profile', str(self.profile),
            '--', *arguments], cwd=self.project, env=self.env, capture_output=True,
            text=True, timeout=10)

    def test_transport_executes_container_directly_with_code_home_and_role(self):
        result = self.call('acp')
        self.assertEqual(result.returncode, 0, result.stderr)
        received = json.loads(result.stdout)
        argv = received['argv']
        self.assertEqual(argv[0], 'exec')
        self.assertEqual(argv[-3:], [str(self.image), self.settings['executable'], 'acp'])
        self.assertIn(str(self.code_home) + ':' + str(self.code_home), argv)
        self.assertIn(str(self.project) + ':' + str(self.project), argv)
        self.assertIn('KIMI_CODE_HOME=' + str(self.code_home), argv)
        self.assertEqual(received['role'], '1')
        self.assertFalse(set(argv) & {'sbatch', 'srun', '--yolo', '--auto', '--dangerously-skip-permissions'})

    def test_arguments_remain_literal_and_no_shell_is_started(self):
        marker = self.root / 'not-created'
        argument = '$(touch ' + str(marker) + ')'
        result = self.call('--prompt', argument)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['argv'][-1], argument)
        self.assertFalse(marker.exists())

    def test_scheduler_program_is_rejected(self):
        for key in ('singularity', 'executable'):
            for name in ('sbatch', 'srun', 'salloc'):
                with self.subTest(key=key, name=name):
                    original = self.settings[key]
                    self.settings[key] = '/usr/bin/' + name
                    result = self.call('--version')
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('scheduler', result.stderr.lower())
                    self.settings[key] = original

    def test_credentials_are_not_allowed_in_command_line_environment_fields(self):
        self.settings['environment']['API_KEY'] = 'test-only-secret'
        result = self.call('--version')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('test-only-secret', result.stderr)


if __name__ == '__main__':
    unittest.main()

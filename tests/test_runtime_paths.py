"""Fresh WSL shells must find installed CLIs without sourcing interactive profiles."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class RuntimePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.project = self.home / 'project'
        self.project.mkdir()
        self.base_bin = self.home / 'base-bin'
        self.base_bin.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.base_bin),
                        CCLOOP_MODEL_METADATA_OFFLINE='1', CCLOOP_MODEL_CACHE=str(self.home / 'cache.json'))
        for name in ('CCLOOP_CODEX_BIN', 'CCLOOP_CLAUDE_BIN', 'CCLOOP_KIMI_BIN'):
            self.env.pop(name, None)
        self.env.pop('NVM_DIR', None)

    def executable(self, relative):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o700)
        return path

    def plan(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / 'launch.py'),
            'Fresh shell goal', '--project', str(self.project), '--offline-models', '--dry-run'],
            env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def resolve(self, name):
        source = ('import sys,shutil;sys.path.insert(0,sys.argv[1]);'
                  'import runtime_paths;runtime_paths.configure();print(shutil.which(sys.argv[2]))')
        result = subprocess.run([sys.executable, '-c', source, str(SCRIPTS), name],
            env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_plain_goal_finds_nvm_codex_without_interactive_shell_path(self):
        self.executable('.nvm/versions/node/v22.22.0/bin/codex')
        plan = self.plan()
        self.assertTrue(plan['available'], plan['reason'])
        self.assertEqual(plan['driver'], 'codex')

    def test_missing_primary_finds_configured_kimi_shim_in_fresh_shell(self):
        self.executable('.local/kimi-shim/kimi')
        plan = self.plan()
        self.assertTrue(plan['available'], plan['reason'])
        self.assertEqual(plan['driver'], 'kimi')

    def test_existing_path_selection_is_preserved(self):
        chosen = self.executable('base-bin/codex')
        self.executable('.nvm/versions/node/v99.0.0/bin/codex')
        self.assertEqual(self.resolve('codex'), str(chosen))

    def test_nvm_versions_are_ordered_numerically(self):
        self.executable('.nvm/versions/node/v9.0.0/bin/codex')
        chosen = self.executable('.nvm/versions/node/v22.0.0/bin/codex')
        self.assertEqual(self.resolve('codex'), str(chosen))

    def test_explicit_invalid_override_is_not_replaced_with_a_different_cli(self):
        self.executable('.nvm/versions/node/v22.22.0/bin/codex')
        self.env['CCLOOP_CODEX_BIN'] = str(self.home / 'missing-requested-codex')
        plan = self.plan()
        self.assertEqual(plan['driver'], 'kimi')
        self.assertTrue(plan['fallback'])
        self.assertFalse(plan['available'])

    def test_explicit_nvm_directory_is_respected(self):
        self.env['NVM_DIR'] = str(self.home / 'custom-nvm')
        chosen = self.executable('custom-nvm/versions/node/v22.0.0/bin/codex')
        self.assertEqual(self.resolve('codex'), str(chosen))

    def test_peer_client_override_expands_home_for_commands_and_doctor(self):
        chosen = self.executable('bin/claude')
        self.env['CCLOOP_CLAUDE_BIN'] = '~/bin/claude'
        source = ('import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
                  'import providers;print(json.dumps({"command":providers.command("claude",'
                  '{"models":providers.MODELS},Path.cwd())[0],'
                  '"doctor":providers.doctor()["providers"]["claude"]["executable"]}))')
        result = subprocess.run([sys.executable, '-c', source, str(SCRIPTS)],
            env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'command': str(chosen), 'doctor': str(chosen)})

    def test_nvm_client_uses_its_own_node_without_changing_parent_path(self):
        old_node = self.executable('base-bin/node')
        old_node.write_text('#!/bin/sh\necho OLD_NODE\n')
        codex = self.executable('.nvm/versions/node/v22.0.0/bin/codex')
        codex.write_text('#!/usr/bin/env node\n')
        node = self.executable('.nvm/versions/node/v22.0.0/bin/node')
        node.write_text('#!/bin/sh\necho PAIRED_NODE_22\n')
        source = ('import sys,os,subprocess;sys.path.insert(0,sys.argv[1]);'
                  'import runtime_paths;runtime_paths.configure();before=os.environ["PATH"];'
                  'p=subprocess.run([sys.argv[2],"--version"],env=runtime_paths.client_env(sys.argv[2]),'
                  'capture_output=True,text=True,check=True);'
                  'assert os.environ["PATH"]==before;print(p.stdout.strip())')
        result = subprocess.run([sys.executable, '-c', source, str(SCRIPTS), str(codex)],
            env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'PAIRED_NODE_22')


if __name__ == '__main__':
    unittest.main()

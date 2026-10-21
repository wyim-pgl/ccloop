"""Provider boundary tests use the same fake executable as ledger tests."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

import test_ccloop as fixtures


class ProviderBoundaryTest(fixtures.CLIBase):
    def test_claude_disables_tools_mcp_hooks_and_parses_structured_output(self):
        self.env['CLAUDECODE'] = '1'
        self.init(driver='codex')
        self.settings('claude', structured=True, criterion='met')
        s = self.consult(stage='final')
        self.assertEqual(s['phase'], 'complete')
        args = self.calls()[0]['argv']
        for flag in ('--safe-mode', '--strict-mcp-config', '--no-session-persistence', '--disable-slash-commands'):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--tools') + 1], '')
        self.assertEqual(json.loads(args[args.index('--mcp-config') + 1]), {'mcpServers': {}})
        self.assertEqual(args[args.index('--permission-mode') + 1], 'dontAsk')
        self.assertEqual(s['last_attempts'][0]['requested_model'], 'claude-fable-5-1')
        self.assertEqual(s['last_attempts'][0]['observed_models'], ['claude-fable-5-1'])
        self.assertIsNone(self.calls()[0]['claudecode'])
        self.assertEqual(self.calls()[0]['reviewer'], '1')

    def test_codex_ignores_host_config_rules_and_reports_unobserved_model_truthfully(self):
        self.init()
        s = self.consult()
        args = self.calls()[0]['argv']
        for flag in ('--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check'):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--sandbox') + 1], 'read-only')
        self.assertEqual(args[args.index('--disable') + 1], 'shell_tool')
        self.assertEqual(args[args.index('-c') + 1], 'model_reasoning_effort="max"')
        self.assertEqual(s['last_attempts'][0]['observed_models'], [])
        self.assertEqual(s['last_attempts'][0]['requested_model'], 'gpt-6-astra')

    def test_kimi_zero_tool_agent_empty_mcp_and_no_plugins(self):
        self.init()
        self.settings('codex', mode='access')
        self.consult()
        call = self.calls()[-1]
        self.assertEqual(call['provider'], 'kimi')
        self.assertIn('tools: []', call['agent'])
        self.assertNotIn('extend:', call['agent'])
        self.assertEqual(call['mcp'], {'mcpServers': {}})
        self.assertEqual(call['skills'], [])
        self.assertIn('--thinking', call['argv'])

    def test_kimi_refuses_global_hooks_before_executable_runs(self):
        self.init()
        self.settings('codex', mode='access')
        kimi = self.home / '.kimi'
        kimi.mkdir()
        (kimi / 'config.toml').write_text('[hooks]\nPreToolUse = [{command="touch forbidden"}]\n')
        self.consult(ok=False)
        self.assertEqual([x['provider'] for x in self.calls()], ['codex'])
        self.assertIn('hook', self.state()['reason'].lower())

    def test_kimi_refuses_installed_plugins_before_executable_runs(self):
        self.init()
        self.settings('codex', mode='access')
        plugin = self.home / '.kimi' / 'plugins' / 'fixture'
        plugin.mkdir(parents=True)
        (plugin / 'plugin.json').write_text('{}')
        self.consult(ok=False)
        self.assertEqual([x['provider'] for x in self.calls()], ['codex'])
        self.assertIn('plugin', self.state()['reason'].lower())

    def test_kimi_custom_config_paths_fail_closed_without_losing_attempt_evidence(self):
        self.init()
        self.settings('codex', mode='access')
        self.env['KIMI_SHARE_DIR'] = str(self.base / 'custom-kimi')
        self.consult(ok=False)
        self.assertEqual([x['provider'] for x in self.calls()], ['codex'])
        self.assertIn('KIMI_SHARE_DIR', self.state()['reason'])
        self.assertEqual(len(self.state()['attempts']), 2)

    def test_native_claude_access_errors_at_exit_zero_use_kimi_once(self):
        messages = [
            'The selected model may not exist or you may not have access to it.',
            "You've hit your limit; resets tomorrow",
            'API Error: 529 overloaded_error',
            'API Error: 500 Internal server error',
        ]
        for index, message in enumerate(messages):
            with self.subTest(message=message):
                self.root = self.base / f'native-{index}'
                self.env['CCLOOP_TEST_ROOT'] = str(self.root)
                self.init(driver='codex')
                self.settings('claude', mode='claude_error', error_message=message)
                previous = len(self.calls())
                self.assertEqual(self.consult()['phase'], 'ready')
                self.assertEqual([x['provider'] for x in self.calls()[previous:]], ['claude', 'kimi'])

    def test_missing_primary_executable_uses_only_kimi(self):
        self.config['commands']['codex'] = [str(self.base / 'missing-cli')]
        self.init()
        self.assertEqual(self.consult()['phase'], 'ready')
        self.assertEqual([x['provider'] for x in self.calls()], ['kimi'])

    def test_nonzero_native_stdout_access_error_survives_stderr_warning(self):
        for code, message in [(401, 'HTTP 401 unauthorized'), (429, 'HTTP 429 rate limit exceeded')]:
            with self.subTest(code=code):
                self.root = self.base / f'error-{code}'
                self.env['CCLOOP_TEST_ROOT'] = str(self.root)
                self.init()
                self.settings('codex', mode='codex_error', error_message=message,
                              stderr='Warning: telemetry is disabled')
                self.settings('kimi', criterion='met')
                previous = len(self.calls())
                s = self.consult(stage='final')
                self.assertEqual(s['phase'], 'complete')
                self.assertEqual([x['provider'] for x in self.calls()[previous:]], ['codex', 'kimi'])
                self.assertEqual(s['last_attempts'][0]['status'], 'access_error')
                self.assertIn(message, s['last_attempts'][0]['error'])
                self.assertIn('telemetry', Path(s['last_attempts'][0]['stderr_path']).read_text())

    def test_nonzero_refusal_malformed_and_valid_report_never_trigger_access_fallback(self):
        for mode in ('codex_error', 'invalid', 'ok'):
            with self.subTest(mode=mode):
                self.root = self.base / f'nonaccess-{mode}'
                self.env['CCLOOP_TEST_ROOT'] = str(self.root)
                self.init()
                self.settings('codex', mode=mode, error_message='Reviewer refused to assess evidence',
                    stderr='Warning: telemetry is disabled', returncode=1, criterion='met',
                    report={'summary': 'Tested HTTP 401 and 429 rate limit handling'})
                previous = len(self.calls())
                self.consult(stage='final', ok=False)
                self.assertEqual([x['provider'] for x in self.calls()[previous:]], ['codex'])
                self.assertEqual(self.state()['phase'], 'blocked')
                self.assertIsNone(self.state().get('last_review'))

    def test_nonzero_plain_stdout_access_diagnostic_with_empty_stderr_uses_fallback(self):
        for index, message in enumerate(['Error: authentication required; please log in', '401 Unauthorized']):
            with self.subTest(message=message):
                self.root = self.base / f'plain-error-{index}'
                self.env['CCLOOP_TEST_ROOT'] = str(self.root)
                self.init()
                self.settings('codex', mode='stdout_access', error_message=message)
                previous = len(self.calls())
                s = self.consult()
                self.assertEqual(s['phase'], 'ready')
                self.assertEqual([x['provider'] for x in self.calls()[previous:]], ['codex', 'kimi'])
                self.assertIn(message, s['last_attempts'][0]['error'])

    def test_run_scoped_model_overrides_do_not_change_configuration_files(self):
        host_config = self.home / '.claude.json'
        host_config.write_text('{"model":"leave-me-alone"}')
        self.init(driver='codex', models={'claude': 'future-frontier'})
        s = self.consult()
        self.assertEqual(s['last_attempts'][0]['requested_model'], 'future-frontier')
        self.assertEqual(host_config.read_text(), '{"model":"leave-me-alone"}')

    def test_doctor_is_read_only_and_never_contacts_models(self):
        result = subprocess.run([sys.executable, str(fixtures.SCRIPT), 'doctor'], env=self.env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(set(output['providers']), {'claude', 'codex', 'kimi'})
        self.assertFalse(self.root.exists())
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()

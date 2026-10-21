"""Launcher integration tests using executable fixtures, never real model CLIs."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'launch.py'
SKILL = SCRIPT.parent.parent / 'SKILL.md'
HELPER = SCRIPT.parent / 'ccloop.py'
FAKE = r'''import json, os, pathlib, sys, time
driver = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
probe = '--print' in args or 'exec' in args
entry = {'driver': driver, 'argv': args, 'cwd': os.getcwd(), 'probe': probe}
if probe:
    entry['stdin'] = sys.stdin.read()
    entry['reviewer'] = os.environ.get('CCLOOP_REVIEWER')
with open(os.environ['LAUNCH_TEST_LOG'], 'a') as log:
    log.write(json.dumps(entry) + '\n')
if probe:
    mode = os.environ.get('LAUNCH_TEST_PROBE_' + driver.upper(), 'ready')
    if mode == 'access':
        print('authentication failed: quota or model access unavailable', file=sys.stderr)
        sys.exit(1)
    if mode == 'provider':
        print('internal assertion failed', file=sys.stderr)
        sys.exit(1)
    if mode == 'invalid':
        print('not valid JSON')
        sys.exit(0)
    if mode == 'wait':
        time.sleep(2)
    if mode == 'wait_for_stop':
        pathlib.Path(os.environ['LAUNCH_TEST_PROBE_PID']).write_text(str(os.getpid()))
        time.sleep(30)
    if mode == 'stop':
        path = pathlib.Path(os.environ['LAUNCH_TEST_STATE'])
        state = json.loads(path.read_text())
        state.update(enabled=False, phase='stopped', failure_kind='stopped',
                     generation=state['generation'] + 1)
        path.write_text(json.dumps(state))
    report = {'status': 'busy' if mode == 'wrong_status' else 'ready'}
    model = args[args.index('--model') + 1]
    if driver == 'claude':
        print(json.dumps({'result': json.dumps(report), 'is_error': False,
                          'modelUsage': {model: {}}}))
    elif driver == 'codex':
        print(json.dumps({'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': json.dumps(report)}}))
    else:
        print(json.dumps({'role': 'assistant', 'model': model,
                          'content': [{'type': 'text', 'text': json.dumps(report)}]}))
    sys.exit(0)
print(driver + ' interactive output', flush=True)
print(driver + ' diagnostic', file=sys.stderr, flush=True)
raise SystemExit(int(os.environ.get('LAUNCH_TEST_EXIT_' + driver.upper(), '0')))
'''


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project with spaces'
        self.project.mkdir()
        self.bin = self.base / 'fake executables'
        self.bin.mkdir()
        self.log = self.base / 'invocations.jsonl'
        self.env = os.environ.copy()
        # Even an incorrect fallback cannot accidentally find a real provider.
        self.env['PATH'] = str(self.bin)
        self.env['LAUNCH_TEST_LOG'] = str(self.log)
        self.env['CCLOOP_MODEL_CACHE'] = str(self.base / 'model-cache.json')
        self.env['CCLOOP_MODEL_METADATA_OFFLINE'] = '1'
        self.test_home = self.base / 'test home'
        self.test_home.mkdir()
        self.env['HOME'] = str(self.test_home)
        self.env.pop('CCLOOP_CONSULTANT', None)
        self.env.pop('CCLOOP_REVIEWER', None)
        self.executables = {}
        for driver in ('claude', 'codex', 'kimi'):
            path = self.bin / driver
            path.write_text(f'#!{sys.executable}\n' + FAKE, encoding='utf-8')
            path.chmod(0o755)
            self.executables[driver] = path
            self.env['CCLOOP_' + driver.upper() + '_BIN'] = str(path)

    def launch(self, driver='claude', goal='Complete the supplied task', *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--driver', driver,
             '--project', str(self.project), '--goal', goal, '--offline-models', *extra],
            cwd=self.base, env=self.env, capture_output=True, text=True, timeout=10,
        )

    def all_calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def invocations(self):
        return [entry for entry in self.all_calls() if not entry['probe']]

    def probes(self):
        return [entry for entry in self.all_calls() if entry['probe']]

    def ledger(self, name, driver, goal, models=None, **changes):
        root = self.base / name
        config = self.base / (name + '.json')
        config.write_text(json.dumps({
            'project': str(self.project), 'driver': driver, 'goal': goal,
            'authorization': 'Synthetic launcher fixture',
            'success_criteria': [{'id': 'task', 'description': 'Complete the fixture task'}],
            'watch': ['**/*.txt'], 'models': models or {}, 'auto_update_models': False,
        }))
        result = subprocess.run([sys.executable, str(HELPER), '--root', str(root),
                                 'init', '--config', str(config)],
                                env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        state_path = root / 'state.json'
        if changes:
            state = json.loads(state_path.read_text())
            state.update(changes)
            state_path.write_text(json.dumps(state))
        return state_path

    def test_default_frontier_flags_reach_each_interactive_driver(self):
        expected = {
            'claude': ['--model', 'claude-fable-5-1', '--effort', 'max'],
            'codex': ['--model', 'gpt-6-astra', '-c', 'model_reasoning_effort="max"'],
            'kimi': ['--model', 'kimi-code/k3', '--thinking', '--prompt'],
        }
        for driver, prefix in expected.items():
            with self.subTest(driver=driver):
                result = self.launch(driver)
                self.assertEqual(result.returncode, 0, result.stderr)
                call = self.invocations()[-1]
                self.assertEqual(call['driver'], driver)
                self.assertEqual(call['argv'][:-1], prefix)
                self.assertEqual(call['cwd'], str(self.project))
                prompt = call['argv'][-1]
                if driver == 'claude':
                    self.assertTrue(prompt.startswith('/ccloop'), prompt)
                elif driver == 'codex':
                    self.assertTrue(prompt.startswith('$ccloop'), prompt)
                else:
                    self.assertIn(str(SKILL), prompt)
                    self.assertIn('Read', prompt)
                for model in ('claude-fable-5-1', 'gpt-6-astra', 'kimi-code/k3'):
                    self.assertIn(model, prompt)

    def test_goal_is_one_verbatim_argument_without_shell_expansion(self):
        sentinel = self.base / 'unexpected-shell-output'
        goal = f'  Fix "quotes"; $(touch {sentinel}) `touch {sentinel}` $HOME\nsecond line  '
        result = self.launch('claude', goal)
        self.assertEqual(result.returncode, 0, result.stderr)
        call = self.invocations()[0]
        self.assertEqual(len(call['argv']), 5)
        self.assertTrue(call['argv'][-1].endswith(goal))
        self.assertFalse(sentinel.exists())

    def test_model_overrides_pin_driver_and_reviewer_instructions(self):
        result = self.launch(
            'codex', 'Use current replacements',
            '--claude-model', 'claude-next', '--codex-model', 'codex-next',
            '--kimi-model', 'kimi-next',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        call = self.invocations()[0]
        self.assertEqual(call['argv'][:2], ['--model', 'codex-next'])
        self.assertIn('claude-next', call['argv'][-1])
        self.assertIn('kimi-next', call['argv'][-1])

    def test_new_run_automatic_model_updates_follow_explicit_override_intent(self):
        for flags, expected in (([], 'true'), (['--claude-model', 'chosen-claude'], 'false')):
            with self.subTest(flags=flags):
                result = self.launch('claude', 'Keep the same goal', *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                prompt = self.invocations()[-1]['argv'][-1]
                self.assertIn('"auto_update_models": ' + expected, prompt)

    def test_root_is_absolute_and_existing_checkpoint_is_preserved(self):
        state_path = self.ledger('private ledger', 'claude', 'Original objective')
        before = state_path.read_bytes()
        result = self.launch('claude', 'Original objective', '--root', 'private ledger')
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.invocations()[0]['argv'][-1]
        self.assertIn(str(self.base / 'private ledger'), prompt)
        self.assertIn('checkpoint', prompt)
        self.assertIn('criteria', prompt)
        self.assertIn('budgets', prompt)
        self.assertIn('resume', prompt.lower())
        self.assertEqual(state_path.read_bytes(), before)

    def test_missing_primary_launches_kimi_once_with_same_goal_and_root(self):
        self.executables['claude'].unlink()
        goal = 'Continue the exact original goal\nwith its second line'
        self.ledger('saved ledger', 'claude', goal)
        result = self.launch('claude', goal, '--root', 'saved ledger')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.invocations()
        self.assertEqual([call['driver'] for call in calls], ['kimi'])
        self.assertEqual(calls[0]['argv'][:-1],
                         ['--model', 'kimi-code/k3', '--thinking', '--prompt'])
        prompt = calls[0]['argv'][-1]
        self.assertTrue(prompt.endswith(goal))
        self.assertIn(str(self.base / 'saved ledger'), prompt)
        self.assertIn(str(SKILL), prompt)
        self.assertIn('handoff', prompt.lower())
        self.assertIn('fallback', result.stderr.lower())
        self.assertIn('claude', result.stderr)

    def test_unlaunchable_exit_status_uses_one_kimi_fallback(self):
        for status in (126, 127):
            with self.subTest(status=status):
                self.log.unlink(missing_ok=True)
                self.env['LAUNCH_TEST_EXIT_CODEX'] = str(status)
                name = 'ledger-' + str(status)
                self.ledger(name, 'codex', 'Preserve objective')
                result = self.launch('codex', 'Preserve objective', '--root', name)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.invocations()
                self.assertEqual([call['driver'] for call in calls], ['codex', 'kimi'])
                self.assertTrue(calls[1]['argv'][-1].endswith('Preserve objective'))
                self.assertIn(str(self.base / name), calls[1]['argv'][-1])

    def test_successful_session_never_starts_fallback(self):
        result = self.launch('claude')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call['driver'] for call in self.invocations()], ['claude'])
        self.assertIn('claude interactive output', result.stdout)
        self.assertIn('claude diagnostic', result.stderr)
        self.assertNotIn('fallback', result.stderr.lower())

    def test_ambiguous_failure_preserves_status_and_requests_explicit_handoff(self):
        self.env['LAUNCH_TEST_EXIT_CODEX'] = '1'
        self.ledger('saved ledger', 'codex', 'Preserve objective')
        result = self.launch('codex', 'Preserve objective', '--root', 'saved ledger')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([call['driver'] for call in self.invocations()], ['codex'])
        self.assertIn('handoff', result.stderr.lower())
        self.assertIn('--driver kimi', result.stderr)
        self.assertIn('saved ledger', result.stderr)
        self.assertNotIn('--claude-model', result.stderr)
        self.assertNotIn('--codex-model', result.stderr)
        self.assertNotIn('--kimi-model', result.stderr)

    def test_user_interrupt_does_not_start_fallback(self):
        self.env['LAUNCH_TEST_EXIT_CLAUDE'] = '130'
        result = self.launch('claude')
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual([call['driver'] for call in self.invocations()], ['claude'])

    def test_kimi_failure_is_reported_without_retry(self):
        self.executables['claude'].unlink()
        self.env['LAUNCH_TEST_EXIT_KIMI'] = '127'
        result = self.launch('claude')
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertEqual([call['driver'] for call in self.invocations()], ['kimi'])
        self.assertIn('claude', result.stderr)
        self.assertIn('kimi', result.stderr)

    def test_missing_primary_and_kimi_fail_clearly_without_any_calls(self):
        self.executables['codex'].unlink()
        self.executables['kimi'].unlink()
        result = self.launch('codex')
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertEqual(self.invocations(), [])
        self.assertIn('codex', result.stderr)
        self.assertIn('kimi', result.stderr)

    def test_missing_selected_kimi_never_falls_back_to_itself(self):
        self.executables['kimi'].unlink()
        result = self.launch('kimi')
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertEqual(self.invocations(), [])
        self.assertIn('kimi', result.stderr)
        self.assertNotIn('fallback', result.stderr.lower())

    def test_dry_run_describes_command_without_starting_any_executable(self):
        result = self.launch('claude', 'Preview exactly', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan['driver'], 'claude')
        self.assertEqual(plan['requested_driver'], 'claude')
        self.assertEqual(plan['argv'][:-1], [str(self.executables['claude']),
                         '--model', 'claude-fable-5-1', '--effort', 'max'])
        self.assertEqual(plan['project'], str(self.project))
        self.assertEqual(plan['models'], {'claude': 'claude-fable-5-1',
                                         'codex': 'gpt-6-astra', 'kimi': 'kimi-code/k3'})
        self.assertFalse(plan['fallback'])
        self.assertTrue(plan['available'])
        self.assertEqual(self.invocations(), [])
        self.assertEqual(self.all_calls(), [])

    def test_dry_run_predicts_missing_executable_fallback(self):
        self.executables['codex'].unlink()
        self.ledger('ledger', 'codex', 'Preview fallback')
        result = self.launch('codex', 'Preview fallback', '--root', 'ledger', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan['requested_driver'], 'codex')
        self.assertEqual(plan['driver'], 'kimi')
        self.assertTrue(plan['fallback'])
        self.assertEqual(plan['root'], str(self.base / 'ledger'))
        self.assertEqual(plan['argv'][0], str(self.executables['kimi']))
        self.assertEqual(self.invocations(), [])
        self.assertEqual(self.all_calls(), [])

    def test_dry_run_reports_unavailable_kimi_without_execution(self):
        self.executables['claude'].unlink()
        self.executables['kimi'].unlink()
        result = self.launch('claude', 'Preview unavailable fallback', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertFalse(plan['available'])
        self.assertTrue(plan['fallback'])
        self.assertIn('kimi', plan['reason'])
        self.assertEqual(self.invocations(), [])
        self.assertEqual(self.all_calls(), [])

    def test_executable_override_is_one_path_not_a_shell_command(self):
        sentinel = self.base / 'unexpected-shell-output'
        self.env['CCLOOP_CLAUDE_BIN'] = f'{self.executables["claude"]}; touch {sentinel}'
        result = self.launch('claude')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call['driver'] for call in self.invocations()], ['kimi'])
        self.assertFalse(sentinel.exists())

    def test_missing_project_fails_before_starting_a_driver(self):
        self.project.rmdir()
        result = self.launch('claude')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('project', result.stderr.lower())
        self.assertEqual(self.invocations(), [])

    def test_readiness_probe_precedes_interactive_launch_without_task_goal(self):
        goal = 'Private task text must never reach the readiness request'
        result = self.launch('claude', goal)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([(entry['driver'], entry['probe']) for entry in self.all_calls()],
                         [('claude', True), ('claude', False)])
        probe = self.probes()[0]
        self.assertNotIn(goal, probe['stdin'])
        self.assertIn('{"status":"ready"}', probe['stdin'])
        self.assertNotEqual(probe['cwd'], str(self.project))
        self.assertEqual(probe['reviewer'], '1')
        self.assertIn('--safe-mode', probe['argv'])
        self.assertEqual(probe['argv'][probe['argv'].index('--tools') + 1], '')
        evidence = [json.loads(line.split(': ', 1)[1]) for line in result.stderr.splitlines()
                    if line.startswith('ccloop readiness: ')]
        self.assertEqual(evidence[0]['requested_model'], 'claude-fable-5-1')
        self.assertEqual(evidence[0]['observed_models'], ['claude-fable-5-1'])

    def test_startup_access_failure_uses_kimi_before_any_primary_interactive_session(self):
        self.env['LAUNCH_TEST_PROBE_CODEX'] = 'access'
        result = self.launch('codex', 'Same original goal')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([(entry['driver'], entry['probe']) for entry in self.all_calls()],
                         [('codex', True), ('kimi', True), ('kimi', False)])
        self.assertTrue(self.invocations()[0]['argv'][-1].endswith('Same original goal'))
        self.assertIn('fallback', result.stderr)

    def test_invalid_readiness_and_provider_failures_do_not_fallback(self):
        for mode in ('invalid', 'wrong_status', 'provider'):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                self.env['LAUNCH_TEST_PROBE_CLAUDE'] = mode
                result = self.launch('claude')
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual([entry['driver'] for entry in self.probes()], ['claude'])
                self.assertEqual(self.invocations(), [])
                self.assertNotIn('starting kimi fallback', result.stderr)

    def test_unsafe_kimi_configuration_blocks_probe_and_interactive_session(self):
        config = self.test_home / '.kimi' / 'config.toml'
        config.parent.mkdir()
        config.write_text('[hooks]\nPreToolUse = [{command="blocked fixture"}]\n')
        result = self.launch('kimi')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.all_calls(), [])
        self.assertIn('hooks', result.stderr)

    def test_access_timeout_is_bounded_and_can_trigger_kimi_fallback(self):
        self.env['LAUNCH_TEST_PROBE_CLAUDE'] = 'wait'
        result = self.launch('claude', 'Keep task', '--access-timeout', '0.2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([entry['driver'] for entry in self.invocations()], ['kimi'])
        self.assertIn('timed out', result.stderr)

    def test_access_timeout_rejects_nonpositive_or_nonfinite_values_without_calls(self):
        for value in ('0', '-1', 'nan', 'inf'):
            with self.subTest(value=value):
                result = self.launch('claude', 'Task', '--access-timeout', value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('positive and finite', result.stderr)
                self.assertEqual(self.all_calls(), [])

    def test_unobserved_model_is_reported_without_inventing_confirmation(self):
        result = self.launch('codex')
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = [json.loads(line.split(': ', 1)[1]) for line in result.stderr.splitlines()
                    if line.startswith('ccloop readiness: ')]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]['requested_model'], 'gpt-6-astra')
        self.assertEqual(evidence[0]['observed_models'], [])

    def test_resume_inherits_frozen_models_without_mutating_the_ledger(self):
        models = {'claude': 'frozen-claude', 'codex': 'frozen-codex', 'kimi': 'frozen-kimi'}
        state_path = self.ledger('frozen', 'claude', 'Frozen goal', models=models)
        before = state_path.read_bytes()
        result = self.launch('claude', 'Frozen goal', '--root', 'frozen')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocations()[0]['argv'][1], 'frozen-claude')
        self.assertIn('frozen-codex', self.invocations()[0]['argv'][-1])
        self.assertEqual(state_path.read_bytes(), before)

    def test_resume_rejects_changed_goal_or_model_before_readiness(self):
        self.ledger('frozen', 'claude', 'Frozen goal')
        cases = [('Changed goal', []), ('Frozen goal', ['--claude-model', 'replacement'])]
        for goal, flags in cases:
            with self.subTest(goal=goal, flags=flags):
                result = self.launch('claude', goal, '--root', 'frozen', *flags)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('frozen', result.stderr.lower())
                self.assertEqual(self.all_calls(), [])

    def test_terminal_or_inflight_ledger_states_block_all_calls(self):
        variants = {
            'stopped': {'enabled': False, 'phase': 'stopped', 'failure_kind': 'stopped'},
            'complete': {'enabled': False, 'phase': 'complete'},
            'pending': {'pending': {'token': 'existing-consultation'}},
            'time-exhausted': {'deadline': time.time() - 1},
            'round-exhausted': {'round': 12},
            'no-progress-exhausted': {'no_progress_reviews': 3},
        }
        for name, changes in variants.items():
            with self.subTest(state=name):
                state_path = self.ledger(name, 'claude', 'Frozen goal', **changes)
                before = state_path.read_bytes()
                result = self.launch('claude', 'Frozen goal', '--root', name)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.all_calls(), [])
                self.assertEqual(state_path.read_bytes(), before)

    def test_access_blocked_root_requires_explicit_helper_handoff_before_probing(self):
        state_path = self.ledger('blocked', 'claude', 'Frozen goal', enabled=False,
                                 phase='blocked', failure_kind='access')
        before = state_path.read_bytes()
        result = self.launch('kimi', 'Frozen goal', '--root', 'blocked')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.all_calls(), [])
        self.assertEqual(state_path.read_bytes(), before)
        self.assertIn('handoff', result.stderr)
        self.assertIn('ccloop.py', result.stderr)

    def test_ready_ledger_handoff_preserves_goal_history_and_remaining_budgets(self):
        state_path = self.ledger('ready', 'claude', 'Frozen goal', round=4,
                                 no_progress_reviews=1)
        before = json.loads(state_path.read_text())
        self.env['LAUNCH_TEST_PROBE_CLAUDE'] = 'access'
        result = self.launch('claude', 'Frozen goal', '--root', 'ready')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([entry['driver'] for entry in self.invocations()], ['kimi'])
        after = json.loads(state_path.read_text())
        self.assertEqual(after['active_driver'], 'kimi')
        for key in ('config', 'round', 'deadline', 'created_at', 'no_progress_reviews'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['history'][:-1], before['history'])
        self.assertEqual(after['history'][-1]['action'], 'handoff')

    def test_user_stop_during_probe_blocks_interactive_launch(self):
        state_path = self.ledger('ready', 'claude', 'Frozen goal')
        self.env['LAUNCH_TEST_STATE'] = str(state_path)
        self.env['LAUNCH_TEST_PROBE_CLAUDE'] = 'stop'
        result = self.launch('claude', 'Frozen goal', '--root', 'ready')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual([entry['driver'] for entry in self.probes()], ['claude'])
        self.assertEqual(self.invocations(), [])
        self.assertFalse(json.loads(state_path.read_text())['enabled'])

    def test_user_stop_promptly_terminates_waiting_readiness_probe(self):
        state_path = self.ledger('waiting', 'claude', 'Frozen goal')
        pid_path = self.base / 'probe.pid'
        self.env['LAUNCH_TEST_PROBE_PID'] = str(pid_path)
        self.env['LAUNCH_TEST_PROBE_CLAUDE'] = 'wait_for_stop'
        child = subprocess.Popen(
            [sys.executable, str(SCRIPT), '--driver', 'claude', '--project', str(self.project),
             '--goal', 'Frozen goal', '--root', str(state_path.parent), '--offline-models'],
            cwd=self.base, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def cleanup():
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    if pid_path.exists():
                        try:
                            os.killpg(int(pid_path.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    child.kill()
                    child.communicate()

        self.addCleanup(cleanup)
        until = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < until and child.poll() is None:
            time.sleep(0.01)
        self.assertTrue(pid_path.exists(), 'Readiness probe did not start')
        probe_pid = int(pid_path.read_text())
        started = time.monotonic()
        stop = subprocess.run(
            [sys.executable, str(HELPER), '--root', str(state_path.parent), 'stop',
             '--reason', 'User stopped the run while readiness was waiting'],
            env=self.env, capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(stop.returncode, 0, stop.stderr)
        stopped_state = state_path.read_bytes()
        try:
            stdout, stderr = child.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            self.fail('User stop did not cancel the waiting readiness probe within 3 seconds')
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(child.returncode, 2, stdout + stderr)
        self.assertIn('cancelled', stderr)
        self.assertEqual([entry['driver'] for entry in self.probes()], ['claude'])
        self.assertEqual(self.invocations(), [])
        self.assertEqual(state_path.read_bytes(), stopped_state)
        self.assertEqual(json.loads(stopped_state)['phase'], 'stopped')
        with self.assertRaises(ProcessLookupError):
            os.kill(probe_pid, 0)

    def test_interactive_failure_without_known_root_requires_saved_checkpoint(self):
        self.env['LAUNCH_TEST_EXIT_CLAUDE'] = '127'
        result = self.launch('claude', 'Original objective')
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertEqual([entry['driver'] for entry in self.invocations()], ['claude'])
        self.assertIn('--root', result.stderr)
        self.assertIn('checkpoint', result.stderr)


if __name__ == '__main__':
    unittest.main()

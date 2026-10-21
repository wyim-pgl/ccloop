"""Exercise the real ledger and child lifecycle using tool-free fake CLIs."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'ccloop.py'
MODEL_WRAPPER = '''import json, os, pathlib, sys, types
sys.path.insert(0, sys.argv.pop(1))
metadata = types.ModuleType('model_updates')
def resolve_models(current=None, **kwargs):
    base = pathlib.Path(os.environ['CCLOOP_TEST_BASE'])
    profile = json.loads((base / 'metadata.json').read_text())
    with (base / 'metadata-calls.jsonl').open('a') as log:
        log.write(json.dumps({'current': current, 'force': kwargs.get('force')}) + '\\n')
    if profile.pop('stop', False):
        import ccloop
        ccloop.change_state(pathlib.Path(os.environ['CCLOOP_TEST_ROOT']), 'stop', 'User stopped during metadata refresh')
    return profile
metadata.resolve_models = resolve_models
sys.modules['model_updates'] = metadata
import ccloop
sys.exit(ccloop.main(sys.argv[1:]))
'''
FAKE = r'''import json, os, pathlib, subprocess, sys, time
provider = sys.argv[1]
args = sys.argv[2:]
base = pathlib.Path(os.environ['CCLOOP_TEST_BASE'])
settings = json.loads((base / 'behavior.json').read_text()).get(provider, {})
packet = json.load(sys.stdin)
root = pathlib.Path(os.environ['CCLOOP_TEST_ROOT'])
state = json.loads((root / 'state.json').read_text())
entry = {'provider': provider, 'argv': args, 'cwd': os.getcwd(),
         'intent': bool(state.get('pending')), 'packet': packet,
         'recursive': os.environ.get('CCLOOP_CONSULTANT'),
         'reviewer': os.environ.get('CCLOOP_REVIEWER'), 'claudecode': os.environ.get('CLAUDECODE')}
if provider == 'kimi':
    entry['agent'] = pathlib.Path(args[args.index('--agent-file')+1]).read_text()
    entry['mcp'] = json.loads(pathlib.Path(args[args.index('--mcp-config-file')+1]).read_text())
    entry['skills'] = list(pathlib.Path(args[args.index('--skills-dir')+1]).iterdir())
with (base / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(entry) + '\n')
mode = settings.get('mode', 'ok')
if settings.get('stderr'):
    print(settings['stderr'], file=sys.stderr)
if mode == 'codex_error':
    print(json.dumps({'type': 'turn.failed', 'error': {'message': settings['error_message']}}))
    sys.exit(1)
if mode == 'stdout_access':
    print(settings['error_message'])
    sys.exit(1)
if mode == 'claude_error':
    print(json.dumps({'type': 'result', 'is_error': True, 'result': settings['error_message'], 'modelUsage': {}}))
    sys.exit(0)
if mode in ('access', 'generic'):
    print('rate limit exceeded' if mode == 'access' else 'unexpected internal assertion', file=sys.stderr)
    sys.exit(1)
if mode in ('wait', 'spawn'):
    if mode == 'spawn':
        subprocess.Popen([sys.executable, '-c', 'import pathlib,sys,time; time.sleep(1); pathlib.Path(sys.argv[1]).write_text("escaped")', str(base / 'escaped')])
    (base / 'ready').write_text('ready')
    while not (base / 'release').exists():
        time.sleep(.02)
if mode == 'drift':
    pathlib.Path(os.environ['CCLOOP_TEST_ARTIFACT']).write_text('changed while reviewing')
if mode == 'invalid':
    print('not a report')
    sys.exit(settings.get('returncode', 0))
prior = packet.get('last_review') or {}
report = {'round': packet['round'], 'artifact_sha256': packet['artifact_sha256'],
          'previous_review_sha256': packet['previous_review_sha256'],
          'summary': 'Checked supplied fixture evidence', 'reconciliation': 'Retained original registers',
          'progress': settings.get('progress', True),
          'checks': [{'method': 'Read supplied excerpt', 'result': 'Fixture evidence reviewed', 'evidence': 'packet.evidence'}],
          'criteria': {x['id']: {'status': settings.get('criterion', 'not_met'), 'evidence': 'packet.evidence'} for x in packet['success_criteria']},
          'claims': prior.get('claims', [{'id': 'c1', 'statement': 'Fixture is supplied', 'status': 'accepted', 'evidence': 'packet.evidence'}]),
          'issues': prior.get('issues', []), 'next_milestone': 'Verify result or finish'}
report.update(settings.get('report', {}))
model = settings.get('observed_model', args[args.index('--model')+1])
if provider == 'claude':
    result = {'type': 'result', 'is_error': False, 'modelUsage': {model: {'inputTokens': 10}}}
    result['structured_output' if settings.get('structured') else 'result'] = report if settings.get('structured') else json.dumps(report)
    print(json.dumps(result))
elif provider == 'codex':
    print(json.dumps({'type': 'thread.started', 'thread_id': 'fake'}))
    print(json.dumps({'type': 'item.completed', 'item': {'id': 'one', 'type': 'agent_message', 'text': json.dumps(report)}}))
    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10}}))
else:
    print(json.dumps({'role': 'assistant', 'content': [{'type': 'text', 'text': json.dumps(report)}], 'model': model}))
sys.exit(settings.get('returncode', 0))
'''


class CLIBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.script = SCRIPT
        self.script_args = []
        self.project = self.base / 'project'
        self.project.mkdir()
        self.artifact = self.project / 'result.txt'
        self.artifact.write_text('fixture result\n')
        self.root = self.base / 'private'
        self.fake = self.base / 'fake_cli.py'
        self.fake.write_text(FAKE)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), CCLOOP_TEST_BASE=str(self.base),
                        CCLOOP_TEST_ROOT=str(self.root), CCLOOP_TEST_ARTIFACT=str(self.artifact))
        self.env.pop('CCLOOP_CONSULTANT', None)
        self.env.pop('CCLOOP_REVIEWER', None)
        for name in ('KIMI_SHARE_DIR', 'KIMI_CONFIG_FILE', 'KIMI_CONFIG_DIR', 'KIMI_HOME'):
            self.env.pop(name, None)
        self.behavior = {}
        self.write('behavior.json', self.behavior)
        self.packet = self.write('packet.json', {'summary': 'Fixture milestone',
            'evidence': [{'path': 'result.txt', 'excerpt': 'fixture result\n'}]})
        self.config = {'project': str(self.project), 'driver': 'claude', 'goal': 'Verify fixture',
            'authorization': 'Synthetic fixture authorization',
            'success_criteria': [{'id': 'correct', 'description': 'Fixture matches evidence'}],
            'watch': ['**/*.txt'], 'max_rounds': 12, 'max_hours': 2,
            'max_no_progress_reviews': 3, 'timeout_seconds': 5,
            'auto_update_models': False,
            'commands': {x: [sys.executable, str(self.fake), x] for x in ('claude', 'codex', 'kimi')}}

    def write(self, name, value):
        path = self.base / name
        path.write_text(json.dumps(value))
        return path

    def init(self, **changes):
        self.config.update(changes)
        return self.call('init', config=self.write('config.json', self.config))

    def settings(self, provider, **values):
        self.behavior[provider] = values
        self.write('behavior.json', self.behavior)

    def command(self, action, **flags):
        args = [sys.executable, str(self.script), *self.script_args, '--root', str(self.root), action]
        for key, item in flags.items():
            args.append('--' + key.replace('_', '-'))
            if item is not True:
                args.append(str(item))
        return args

    def call(self, action, ok=True, **flags):
        result = subprocess.run(self.command(action, **flags), env=self.env, cwd=self.base,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0 if ok else 2, result.stdout + result.stderr)
        return json.loads(result.stdout) if ok else result

    def consult(self, stage='milestone', **kwargs):
        return self.call('consult', packet=self.packet, stage=stage, **kwargs)

    def state(self):
        return json.loads((self.root / 'state.json').read_text())

    def calls(self):
        path = self.base / 'calls.jsonl'
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def start_waiting(self, mode='wait'):
        self.settings('codex', mode=mode)
        child = subprocess.Popen(self.command('consult', packet=self.packet, stage='final'),
            env=self.env, cwd=self.base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        end = time.monotonic() + 5
        while not (self.base / 'ready').exists() and time.monotonic() < end and child.poll() is None:
            time.sleep(.01)
        self.assertTrue((self.base / 'ready').exists(), 'fake reviewer never started')
        return child


class ConsultationTest(CLIBase):
    def metadata_stub(self, **updates):
        self.script = self.base / 'metadata_cli.py'
        self.script.write_text(MODEL_WRAPPER)
        self.script_args = [str(SCRIPT.parent)]
        profile = {'models': {'claude': 'claude-next', 'codex': 'codex-next', 'kimi': 'kimi-next'},
                   'checked_at': time.time(), 'sources': ['Synthetic public metadata'],
                   'warnings': [], 'refreshed': True}
        profile.update(updates)
        self.write('metadata.json', profile)

    def test_automatic_model_updates_treat_full_initial_profile_as_seed_and_preserve_config(self):
        self.metadata_stub()
        initial_models = {'claude': 'seed-claude', 'codex': 'seed-codex', 'kimi': 'seed-kimi'}
        s = self.init(auto_update_models=True, models=initial_models)
        self.assertEqual(s['active_models']['claude'], 'claude-next')
        self.assertEqual(s['active_models']['codex'], 'codex-next')
        self.assertEqual(s['config']['models'], initial_models)
        self.consult(stage='plan')
        self.assertEqual(self.calls()[0]['argv'][self.calls()[0]['argv'].index('--model') + 1], 'codex-next')
        self.assertEqual(self.state()['config'], s['config'])
        self.assertEqual(self.state()['deadline'], s['deadline'])
        self.assertEqual(len((self.base / 'metadata-calls.jsonl').read_text().splitlines()), 1)

    def test_disabled_model_updates_keep_initial_profile_when_metadata_is_refreshed_explicitly(self):
        self.metadata_stub()
        initialized = self.init(auto_update_models=False, models={'claude': 'fixed-claude'})
        self.assertFalse((self.base / 'metadata-calls.jsonl').exists())
        refreshed = self.call('models', refresh=True)
        self.assertEqual(refreshed['active_models'], initialized['config']['models'])
        self.assertEqual(refreshed['model_metadata']['models']['claude'], 'claude-next')

    def test_doctor_uses_active_models_instead_of_frozen_initial_profile(self):
        self.metadata_stub()
        self.init(auto_update_models=True)
        checked = self.call('doctor')
        self.assertEqual(checked['providers']['codex']['requested_model'], 'codex-next')

    def test_due_model_refresh_is_audited_only_when_selection_changes(self):
        self.metadata_stub(checked_at=0)
        self.init(auto_update_models=True)
        self.metadata_stub(models={'claude': 'claude-next', 'codex': 'codex-newer', 'kimi': 'kimi-next'})
        self.consult()
        self.assertEqual(self.state()['active_models']['codex'], 'codex-newer')
        audits = [x for x in self.state()['history'] if x['action'] == 'model_refresh']
        self.assertEqual(len(audits), 1)
        self.call('models', refresh=True)
        self.assertEqual(len([x for x in self.state()['history'] if x['action'] == 'model_refresh']), 1)
        self.assertEqual(len((self.base / 'metadata-calls.jsonl').read_text().splitlines()), 3)

    def test_stop_during_metadata_lookup_prevents_review_dispatch(self):
        self.metadata_stub(checked_at=0)
        self.init(auto_update_models=True)
        self.metadata_stub(stop=True)
        self.consult(ok=False)
        self.assertEqual(self.state()['phase'], 'stopped')
        self.assertEqual(self.calls(), [])
        self.assertIsNone(self.state()['pending'])

    def test_model_refresh_revokes_launchers_previous_generation(self):
        self.metadata_stub()
        initial = self.init(auto_update_models=True)
        self.metadata_stub(models={'claude': 'claude-next', 'codex': 'codex-newer', 'kimi': 'kimi-next'})
        self.call('models', refresh=True)
        result = self.guarded_handoff(initial['generation'])
        self.assertNotEqual(result.returncode, 0, 'Old model selection still authorized automatic handoff')
        self.assertEqual(self.state()['active_driver'], 'claude')

    def test_review_interval_metadata_does_not_delay_final_review(self):
        initialized = self.init()
        self.assertEqual(initialized['config'].get('review_interval_minutes'), 30)
        self.assertTrue(initialized['review_due'])
        plan = self.consult(stage='plan')
        self.assertFalse(plan['review_due'])
        self.assertAlmostEqual(plan['next_review_at'] - plan['last_review_at'], 1800, places=4)
        self.settings('codex', criterion='met')
        done = self.consult(stage='final')
        self.assertEqual(done['phase'], 'complete')
        self.assertFalse(done['review_due'])

    def test_heartbeat_preserves_original_goal_without_dispatching(self):
        self.init()
        result = subprocess.run(self.command('heartbeat'), env=self.env, cwd=self.base,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Verify fixture', result.stdout)
        self.assertIn('review_due', result.stdout)
        self.assertIn(str(self.root), result.stdout)
        self.assertIn('ccloop', result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.state()['round'], 0)

    def guarded_handoff(self, generation):
        code = ('import pathlib,sys; sys.path.insert(0,sys.argv[1]); '
                'from ccloop import change_state; '
                'change_state(pathlib.Path(sys.argv[2]), "handoff", "Startup access failure", '
                'driver="kimi", expected_generation=int(sys.argv[3]))')
        return subprocess.run([sys.executable, '-c', code, str(SCRIPT.parent), str(self.root), str(generation)],
                              env=self.env, cwd=self.base, capture_output=True, text=True, timeout=5)

    def test_automatic_handoff_guard_cannot_revive_a_user_stop(self):
        initialized = self.init()
        self.call('stop', reason='User stopped during startup probe')
        result = self.guarded_handoff(initialized['generation'])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()['phase'], 'stopped')
        self.assertEqual(self.state()['active_driver'], 'claude')

    def test_automatic_handoff_guard_accepts_only_current_ready_generation(self):
        initialized = self.init()
        result = self.guarded_handoff(initialized['generation'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state()['active_driver'], 'kimi')
        self.assertGreater(self.state()['generation'], initialized['generation'])

    def test_both_directions_use_frozen_packet_and_only_final_completes(self):
        for driver, reviewer in [('claude', 'codex'), ('codex', 'claude')]:
            with self.subTest(driver=driver):
                self.root = self.base / ('private-' + driver)
                self.env['CCLOOP_TEST_ROOT'] = str(self.root)
                self.init(driver=driver)
                self.settings(reviewer, criterion='met')
                plan = self.consult(stage='plan')
                self.assertEqual(plan['phase'], 'ready')
                done = self.consult(stage='final')
                self.assertEqual(done['phase'], 'complete')
                self.assertFalse(done['enabled'])
                call = self.calls()[-1]
                self.assertEqual(call['provider'], reviewer)
                self.assertTrue(call['intent'])
                self.assertEqual(call['recursive'], '1')
                self.assertNotEqual(Path(call['cwd']), self.project)
                self.assertEqual(call['packet']['packet'], json.loads(self.packet.read_text()))
                self.assertEqual(call['packet']['previous_review_sha256'], plan['review_sha256'])
                self.assertIn('next_milestone', call['packet']['report_schema']['required'])
                self.assertEqual(done['config']['driver'], driver)
                self.assertEqual(done['round'], 2)

    def test_access_failure_falls_back_once_and_records_both_attempts(self):
        self.init()
        self.settings('codex', mode='access')
        self.settings('kimi', criterion='met')
        s = self.consult(stage='final')
        self.assertEqual(s['phase'], 'complete')
        self.assertEqual([x['provider'] for x in self.calls()], ['codex', 'kimi'])
        self.assertEqual([x['status'] for x in s['last_attempts']], ['access_error', 'accepted'])
        self.assertEqual(s['last_attempts'][1]['observed_models'], ['kimi-code/k3'])

    def test_two_access_failures_stop_with_both_reasons(self):
        self.init()
        self.settings('codex', mode='access')
        self.settings('kimi', mode='access')
        self.consult(ok=False)
        s = self.state()
        self.assertEqual(s['phase'], 'blocked')
        self.assertEqual(s['failure_kind'], 'access')
        self.assertIn('codex', s['reason'])
        self.assertIn('kimi', s['reason'])
        self.assertEqual(len(self.calls()), 2)

    def test_invalid_and_generic_failures_never_use_fallback(self):
        for mode in ('invalid', 'generic'):
            with self.subTest(mode=mode):
                self.root = self.base / mode
                self.env['CCLOOP_TEST_ROOT'] = str(self.root)
                self.init()
                self.settings('codex', mode=mode)
                previous = len(self.calls())
                self.consult(ok=False)
                self.assertEqual(len(self.calls()), previous + 1)
                self.assertEqual(self.state()['phase'], 'blocked')
                self.assertNotEqual(self.state()['failure_kind'], 'access')

    def test_claude_model_substitution_is_rejected_and_uses_explicit_kimi_fallback(self):
        self.init(driver='codex')
        self.settings('claude', observed_model='cheaper-model', criterion='met')
        self.settings('kimi', criterion='met')
        self.assertEqual(self.consult(stage='final')['phase'], 'complete')
        self.assertEqual([x['provider'] for x in self.calls()], ['claude', 'kimi'])
        self.assertEqual(self.state()['attempts'][0]['observed_models'], ['cheaper-model'])
        self.assertEqual(self.state()['attempts'][0]['status'], 'access_error')

    def test_drift_while_reviewing_cannot_authorize_work(self):
        self.init()
        self.settings('codex', mode='drift', criterion='met')
        self.consult(stage='final', ok=False)
        s = self.state()
        self.assertEqual(s['phase'], 'blocked')
        self.assertIsNone(s.get('last_review'))
        self.assertIn('changed', s['reason'].lower())

    def test_claim_continuity_and_every_original_criterion_are_validated(self):
        self.init()
        self.consult()
        accepted = self.state()['review_sha256']
        self.settings('codex', report={'claims': []}, criterion='met')
        self.consult(stage='final', ok=False)
        self.assertEqual(self.state()['review_sha256'], accepted)
        self.call('resume', reason='User requested a corrected review')
        self.settings('codex', report={'criteria': {}})
        self.consult(stage='final', ok=False)
        self.assertEqual(self.state()['review_sha256'], accepted)

    def test_blocking_issue_prevents_final_completion(self):
        self.init()
        self.settings('codex', criterion='met', report={'issues': [{'id': 'i1', 'statement': 'Unverified fixture',
            'status': 'open', 'blocking': True, 'evidence': 'Missing independent check'}]})
        self.assertEqual(self.consult(stage='final')['phase'], 'ready')

    def test_round_and_no_progress_budgets_stop_incomplete_run(self):
        self.init(max_rounds=12, max_no_progress_reviews=1)
        self.settings('codex', progress=False)
        self.assertEqual(self.consult(stage='plan')['no_progress_reviews'], 0)
        s = self.consult()
        self.assertEqual(s['phase'], 'blocked')
        self.assertEqual(s['round'], 2)
        self.assertEqual(s['dispatch_limit'], 'No-progress limit reached')
        self.call('resume', ok=False, reason='Cannot reset exhausted budget')
        self.call('handoff', ok=False, driver='kimi', reason='Cannot bypass exhausted budget')
        self.consult(ok=False)
        self.assertEqual(len(self.calls()), 2)

    def test_round_limit_is_enforced_even_with_progress(self):
        self.init(max_rounds=1)
        s = self.consult()
        self.assertEqual(s['phase'], 'blocked')
        self.assertEqual(s['dispatch_limit'], 'Round limit reached')
        self.consult(ok=False)
        self.assertEqual(len(self.calls()), 1)

    def test_expired_wallclock_budget_prevents_any_dispatch(self):
        self.init(max_hours=.000001)
        self.consult(ok=False)
        self.assertEqual(self.state()['phase'], 'blocked')
        self.assertEqual(self.state()['failure_kind'], 'limit')
        self.assertEqual(self.calls(), [])

    def test_single_flight_and_stop_resume_reject_old_result(self):
        self.init()
        child = self.start_waiting()
        self.consult(ok=False)
        self.call('handoff', ok=False, driver='kimi', reason='Cannot change in flight')
        stopped = self.call('stop', reason='User asked to stop')
        self.assertEqual(stopped['phase'], 'stopped')
        self.call('resume', reason='User asked to continue')
        (self.base / 'release').write_text('release')
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 2, out + err)
        self.assertIsNone(self.state().get('last_review'))
        self.settings('codex', criterion='met')
        self.assertEqual(self.consult(stage='final')['phase'], 'complete')

    def test_timeout_kills_process_group_and_allows_access_fallback(self):
        self.init(timeout_seconds=.2)
        self.settings('codex', mode='spawn')
        self.settings('kimi', criterion='met')
        self.assertEqual(self.consult(stage='final')['phase'], 'complete')
        time.sleep(1.1)
        self.assertFalse((self.base / 'escaped').exists())

    def test_interruption_preserves_attempt_and_resume_does_not_replay(self):
        self.init()
        child = self.start_waiting()
        child.send_signal(signal.SIGTERM)
        child.communicate(timeout=5)
        s = self.state()
        self.assertEqual(s['phase'], 'blocked')
        self.assertEqual(s['attempts'][0]['status'], 'interrupted')
        self.assertTrue(Path(s['attempts'][0]['stdout_path']).is_file())
        self.call('resume', reason='User confirmed reviewer interruption')
        self.assertEqual(len(self.calls()), 1)
        self.assertIsNone(self.state().get('pending'))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Safe orphan signalling requires Linux process birth identity')
    def test_resume_reconciles_killed_helper_and_terminates_orphan_reviewer(self):
        self.init()
        child = self.start_waiting(mode='spawn')
        child.kill()
        child.communicate(timeout=5)
        self.assertIsNotNone(self.state()['pending'])
        self.call('resume', reason='User requested recovery after helper was killed')
        self.assertIsNone(self.state()['pending'])
        self.assertEqual(self.state()['attempts'][0]['status'], 'interrupted')
        self.assertEqual(len(self.calls()), 1)
        time.sleep(1.1)
        self.assertFalse((self.base / 'escaped').exists())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Safe orphan signalling requires Linux process birth identity')
    def test_stop_terminates_orphan_reviewer_without_resume_and_preserves_dispatch_audit(self):
        self.init()
        child = self.start_waiting(mode='spawn')
        child.kill()
        child.communicate(timeout=5)
        before = self.state()
        pid = before['attempts'][0]['pid']

        def cleanup_orphan():
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.addCleanup(cleanup_orphan)
        self.call('stop', reason='User stopped after helper crash')
        time.sleep(1.1)
        self.assertFalse((self.base / 'escaped').exists(), 'Orphan descendant survived stop')
        after = self.state()
        self.assertEqual(after['phase'], 'stopped')
        self.assertFalse(after['enabled'])
        self.assertIsNone(after['pending'])
        self.assertEqual(after['attempts'][0]['status'], 'interrupted')
        self.assertIn('User stopped after helper crash', after['attempts'][0]['error'])
        self.assertEqual(after['history'][-1]['cancelled_pending'], before['pending'])
        self.assertTrue(Path(after['attempts'][0]['stdout_path']).exists())
        self.assertEqual(len(self.calls()), 1)

    def test_kimi_driver_uses_independent_reviewers_and_handoff_preserves_goal(self):
        self.init()
        before = self.state()['config']
        self.call('handoff', driver='kimi', reason='Claude driver quota exhausted')
        self.settings('claude', mode='access')
        self.settings('codex', criterion='met')
        s = self.consult(stage='final')
        self.assertEqual(s['phase'], 'complete')
        self.assertEqual(s['active_driver'], 'kimi')
        self.assertEqual(s['config'], before)
        self.assertEqual([x['provider'] for x in self.calls()], ['claude', 'codex'])
        self.call('handoff', ok=False, driver='claude', reason='Completed cannot revive')

    def test_configuration_is_immutable_private_and_rejects_project_state(self):
        s = self.init()
        self.assertTrue((self.root / 'CONTROL.md').is_file(), 'Missing durable control manifest')
        self.assertIn('Verify fixture', (self.root / 'CONTROL.md').read_text())
        for name in ('reviews', 'prompts', 'observations'):
            self.assertTrue((self.root / name).is_dir())
        self.consult()
        self.assertEqual(len(list((self.root / 'reviews').glob('*.json'))), 1)
        self.assertEqual(len(list((self.root / 'prompts').glob('*.json'))), 1)
        self.write('config.json', {'goal': 'tampered'})
        self.assertEqual(self.call('status')['config'], s['config'])
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.call('init', config=self.base / 'config.json', ok=False)
        self.root = self.project / 'ledger'
        self.call('init', config=self.write('config2.json', self.config), ok=False)
        self.assertFalse(self.root.exists())

    def test_nested_consultation_is_refused_before_dispatch(self):
        self.init()
        self.env['CCLOOP_CONSULTANT'] = '1'
        self.consult(ok=False)
        self.assertEqual(self.calls(), [])

    def test_reviewer_environment_marker_also_prevents_recursive_dispatch(self):
        self.init()
        self.env['CCLOOP_REVIEWER'] = '1'
        self.consult(ok=False)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()

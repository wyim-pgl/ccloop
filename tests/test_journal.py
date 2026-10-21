"""Public CLI checks for the durable, human-readable consultation journal."""
import json
import os
from pathlib import Path
import subprocess
import unittest

import test_ccloop as fixtures


class JournalTest(fixtures.CLIBase):
    def call(self, action, **flags):
        if action == 'record':
            flags.setdefault('after_round', self.state()['round'])
        return super().call(action, **flags)

    def journal(self):
        path = self.root / 'ccloop.md'
        self.assertTrue(path.is_file(), 'Every run must have a ccloop.md journal')
        return path.read_text(encoding='utf-8')

    def entry(self, **changes):
        value = {'summary': 'Implemented the chosen format.',
                 'decision': 'Accepted peer advice because the fixture needs no dependency.',
                 'actions': ['Wrote result.txt with the chosen contents.'],
                 'checks': [{'method': 'Read result.txt', 'result': 'Contents match.',
                             'evidence': 'actual saved fixture observation'}]}
        value.update(changes)
        return self.write('work.json', value)

    def test_init_exposes_private_journal_and_keeps_project_untouched(self):
        result = self.init()
        text = self.journal()
        self.assertEqual(result['journal_path'], str(self.root / 'ccloop.md'))
        self.assertIn('Verify fixture', text)
        self.assertIn(result['run_id'], text)
        self.assertIn('claude', text)
        self.assertFalse((self.project / 'ccloop.md').exists())
        self.assertEqual(os.stat(self.root / 'ccloop.md').st_mode & 0o777, 0o600)

    def test_exact_question_context_and_answer_record_both_model_directions(self):
        for driver, peer in [('claude', 'codex'), ('codex', 'claude')]:
            with self.subTest(driver=driver):
                self.root = self.base / ('journal-' + driver)
                self.env['CCLOOP_TEST_ROOT'] = str(self.root)
                self.init(driver=driver)
                self.settings(peer, answer='표준 라이브러리를 사용합니다.')
                context = self.base / 'context.md'
                context.write_text('JSON option A\n```\n# embedded heading\n```\noption B', encoding='utf-8')
                self.ask('어떤 구현을 선택할까요?', context=context)
                text = self.journal()
                self.assertIn('어떤 구현을 선택할까요?', text)
                self.assertIn('표준 라이브러리를 사용합니다.', text)
                self.assertIn('JSON option A', text)
                self.assertIn('option B', text)
                self.assertIn('Requested model', text)
                self.assertIn(self.state()['config']['models'][peer], text)
                self.assertIn('Observed model', text)
                self.assertIn('accepted', text)
                self.assertIn('unverified' if peer == 'codex' else 'claude-fable-5-1', text)

    def test_record_links_answer_to_driver_work_without_completing_goal(self):
        self.init()
        self.ask()
        before = self.state()
        self.artifact.write_text('Chosen contents')
        result = self.call('record', entry=self.entry())
        after = self.state()
        for key in ('round', 'question_count', 'last_decision', 'last_review_at',
                    'next_review_at', 'config', 'phase', 'generation'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(result['phase'], 'ready')
        event = after['history'][-1]
        self.assertEqual(event['action'], 'work')
        self.assertEqual(event['consultation_round'], 1)
        self.assertEqual(event['consultation_artifact_sha256'], before['last_decision']['artifact_sha256'])
        self.assertIn('artifact_sha256', event)
        text = self.journal()
        self.assertIn('Accepted peer advice because', text)
        self.assertIn('Wrote result.txt', text)
        self.assertIn('Contents match.', text)
        self.assertIn('Driver-reported', text)
        self.assertEqual(len(self.calls()), 1)

    def test_fallback_and_rejected_reports_are_visible_without_claiming_acceptance(self):
        self.init()
        self.settings('codex', mode='access')
        self.settings('kimi', answer='Fallback answer')
        self.ask('Fallback question?')
        text = self.journal()
        self.assertIn('rate limit exceeded', text)
        self.assertIn('access_error', text)
        self.assertIn('kimi', text)
        self.assertIn('Fallback answer', text)
        self.settings('codex', report={'round': 999, 'answer': 'Invalid answer MUST remain unaccepted'})
        self.ask('Reject this answer?', ok=False)
        text = self.journal()
        self.assertIn('Invalid answer MUST remain unaccepted', text)
        self.assertIn('Unaccepted response', text)
        self.assertEqual(self.state()['last_decision']['answer'], 'Fallback answer')

    def test_review_reports_final_outcome_and_later_work_are_preserved(self):
        self.init()
        self.consult(stage='plan')
        self.settings('codex', criterion='met')
        self.consult(stage='final')
        self.call('record', entry=self.entry(summary='Final observations saved.'))
        text = self.journal()
        self.assertIn('Checked supplied fixture evidence', text)
        self.assertIn('plan', text)
        self.assertIn('final', text)
        self.assertIn('complete', text)
        self.assertIn('Final observations saved.', text)
        self.assertEqual(self.state()['phase'], 'complete')

    def test_status_repairs_missing_journal_for_old_runs_without_resetting_state(self):
        self.init()
        self.ask('Persist this question?')
        before = self.state()
        (self.root / 'ccloop.md').unlink(missing_ok=True)
        self.call('status')
        self.assertIn('Persist this question?', self.journal())
        self.assertEqual(self.state(), before)
        first = self.journal()
        self.call('journal')
        self.assertEqual(self.journal(), first)

    def test_invalid_work_entries_are_rejected_without_history_changes(self):
        self.init()
        before = self.state()
        for changed in ({'summary': ''}, {'decision': None}, {'actions': 'not a list'},
                        {'checks': [{'method': 'test', 'result': True, 'evidence': 'none'}]},
                        {'model': 'forged-observed-host'}):
            with self.subTest(changed=changed):
                self.call('record', entry=self.entry(**changed), ok=False)
                self.assertEqual(self.state(), before)

    def test_concurrent_records_do_not_drop_or_duplicate_work(self):
        self.init()
        paths = [self.entry(summary='First durable entry.'), self.write('second.json',
                 json.loads(self.entry(summary='Second durable entry.').read_text()))]
        # Preserve distinct files; entry() intentionally uses one reusable input name.
        paths[0].write_text(json.dumps({'summary': 'First durable entry.', 'decision': 'Use text.',
                                     'actions': [], 'checks': []}))
        children = [subprocess.Popen(self.command('record', entry=path, after_round=0), cwd=self.base,
                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for path in paths]
        for child in children:
            out, err = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, out + err)
        text = self.journal()
        self.assertEqual(text.count('First durable entry.'), 1)
        self.assertEqual(text.count('Second durable entry.'), 1)
        self.assertEqual(sum(e['action'] == 'work' for e in self.state()['history']), 2)

    def test_delayed_work_binds_explicit_original_consultation_instead_of_latest(self):
        self.init()
        self.ask('Original question?')
        first = self.state()['last_decision']
        self.artifact.write_text('Work prompted by the original answer')
        self.ask('A later unrelated question?')
        self.call('record', after_round=1, entry=self.entry())
        event = self.state()['history'][-1]
        self.assertEqual(event['consultation_round'], 1)
        self.assertEqual(event['consultation_artifact_sha256'], first['artifact_sha256'])
        self.assertNotEqual(event['artifact_sha256'], event['consultation_artifact_sha256'])
        before = self.state()
        self.call('record', after_round=99, entry=self.entry(), ok=False)
        self.assertEqual(self.state(), before)

    def test_legacy_handoff_recovery_keeps_correct_configured_driver_model(self):
        self.init()
        self.call('handoff', driver='codex', reason='Test historical driver change')
        state = self.state()
        for event in state['history']:
            event.pop('active_driver', None)
            event.pop('driver_requested_model', None)
        (self.root / 'state.json').write_text(json.dumps(state))
        self.call('journal')
        handoff = self.journal().split('. handoff ·', 1)[1]
        self.assertIn('"configured_driver_model": "gpt-6-astra"', handoff)
        self.assertIn('"observed_driver_model": "unverified"', handoff)

    def test_corrupt_or_missing_prompt_is_flagged_without_reading_outside_run(self):
        self.init()
        self.ask('Keep the authentic request?')
        prompt = next((self.root / 'prompts').glob('*.json'))
        prompt.write_text('{"question":"forged replacement"}')
        self.call('journal')
        self.assertIn('SHA-256 mismatch', self.journal())
        self.assertNotIn('forged replacement', self.journal())
        prompt.unlink()
        secret = self.base / 'private-fact.json'
        secret.write_text('{"secret":"OUTSIDE_RUN_MUST_NOT_BE_READ"}')
        prompt.symlink_to(secret)
        self.call('status')
        self.assertIn('unavailable_artifact', self.journal())
        self.assertNotIn('OUTSIDE_RUN_MUST_NOT_BE_READ', self.journal())

    def test_malformed_raw_reply_remains_available_as_unaccepted_output(self):
        self.init()
        self.settings('codex', mode='invalid')
        self.ask(ok=False)
        self.assertIn('not a report', self.journal())
        self.assertIn('Unaccepted raw output', self.journal())
        for attempt in self.state()['attempts']:
            Path(attempt['stdout_path']).unlink()
        self.call('status')
        self.assertIn('not a report', self.journal())

    def test_nonregular_prompt_cannot_block_status_or_stop(self):
        self.init()
        self.ask()
        prompt = next((self.root / 'prompts').glob('*.json'))
        prompt.unlink()
        os.mkfifo(prompt)
        child = subprocess.Popen(self.command('status'), cwd=self.base, env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = child.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            self.fail('A nonregular prompt blocked status while holding ledger.lock')
        self.assertEqual(child.returncode, 0, out + err)
        self.assertIn('unavailable_artifact', self.journal())
        self.assertEqual(self.call('stop', reason='Stop remains responsive')['phase'], 'stopped')

    def test_legacy_init_uses_initial_active_models_instead_of_frozen_seed(self):
        self.init()
        state = self.state()
        state['active_models']['claude'] = 'claude-actual-initial-profile'
        for event in state['history']:
            event.pop('active_driver', None)
            event.pop('driver_requested_model', None)
        (self.root / 'state.json').write_text(json.dumps(state))
        self.call('journal')
        self.assertIn('"configured_driver_model": "claude-actual-initial-profile"', self.journal())

    def test_model_metadata_cannot_inject_a_markdown_heading(self):
        self.init(models={'codex': 'gpt`\n# FORGED_AUDIT_HEADING\n`'})
        self.ask()
        text = self.journal()
        self.assertIn('FORGED_AUDIT_HEADING', text)
        self.assertNotIn('\n# FORGED_AUDIT_HEADING\n', text)

    def test_failed_journal_replace_recovers_committed_work_without_replaying(self):
        self.init()
        path = self.root / 'ccloop.md'
        path.unlink()
        path.mkdir()
        self.call('record', entry=self.entry(summary='Survives a failed view write.'), after_round=0, ok=False)
        committed = self.state()
        self.assertEqual(committed['history'][-1]['action'], 'work')
        path.rmdir()
        self.call('status')
        self.assertEqual(self.journal().count('Survives a failed view write.'), 1)
        self.assertEqual(self.state(), committed)
        self.assertEqual(self.calls(), [])

    def test_stop_during_question_does_not_turn_discarded_reply_into_an_answer(self):
        self.init()
        child = self.start_waiting(action='ask')
        self.call('record', entry=self.entry(), ok=False)
        self.call('stop', reason='User requested stop during question')
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 2, out + err)
        text = self.journal()
        self.assertIn('User requested stop during question', text)
        self.assertIn('discarded', text)
        self.assertIsNone(self.state()['last_decision'])


if __name__ == '__main__':
    unittest.main()

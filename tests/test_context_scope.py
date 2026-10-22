"""Ignore only explicitly excluded generated artifacts during managed recovery."""
import test_ccloop as fixtures


class ContextScopeTest(fixtures.CLIBase):
    def cache(self):
        folder = self.project / 'graft' / '.cache'
        folder.mkdir(parents=True)
        path = folder / 'stats.json'
        path.write_text('{"reads":0}')
        return path

    def test_explicit_cache_exclusion_preserves_review_and_checkpoint_identity(self):
        cache = self.cache()
        self.init(watch=['**/*'], watch_exclude=['graft/.cache/**', 'graft/.graph/**'])
        packet = self.write('checkpoint.json', {'summary': 'Inspected the real fixture.',
            'next_steps': ['Verify result.txt'], 'completed': [], 'working_notes': ''})
        before = self.call('checkpoint', packet=packet)['driver_checkpoint']['artifact_sha256']
        self.env['CCLOOP_TEST_ARTIFACT'] = str(cache)
        self.settings('codex', mode='drift', criterion='met')
        result = self.consult(stage='final')
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(result['last_review']['artifact_sha256'], before)

    def test_cache_is_watched_when_not_explicitly_excluded(self):
        cache = self.cache()
        self.init(watch=['**/*'])
        self.env['CCLOOP_TEST_ARTIFACT'] = str(cache)
        self.settings('codex', mode='drift', criterion='met')
        self.consult(stage='final', ok=False)
        self.assertEqual(self.state()['phase'], 'blocked')

    def test_exclusions_keep_material_artifact_drift_blocking(self):
        self.cache()
        self.init(watch=['**/*'], watch_exclude=['graft/.cache/**'])
        self.settings('codex', mode='drift', criterion='met')
        self.consult(stage='final', ok=False)
        self.assertIn('changed', self.state()['reason'])

    def test_exclusion_paths_must_stay_relative_to_the_project(self):
        for value in ('../secret', '/outside', ''):
            with self.subTest(value=value):
                self.call('init', config=self.write('bad.json', dict(self.config, watch_exclude=[value])), ok=False)

    def test_checkpoint_journal_retains_previous_reported_work_after_new_checkpoint(self):
        self.init()
        for summary in ('FIRST_REPORTED_WORK_811', 'SECOND_REPORTED_WORK_812'):
            packet = self.write('checkpoint.json', {'summary': summary,
                'next_steps': ['Continue the original goal'], 'completed': ['Inspected actual fixture'],
                'working_notes': 'Reported work remains distinct from verified criteria.'})
            self.call('checkpoint', packet=packet)
        text = (self.root / 'ccloop.md').read_text()
        self.assertIn('FIRST_REPORTED_WORK_811', text)
        self.assertIn('SECOND_REPORTED_WORK_812', text)


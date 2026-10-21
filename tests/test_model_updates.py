"""Model discovery uses metadata fixtures; these tests never call a model or network."""
import importlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
HAS_TOMLLIB = importlib.util.find_spec('tomllib') is not None
TOMLLIB_REQUIREMENT = 'Configured Kimi alias discovery requires Python 3.11+ tomllib'


class ModelUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('model_updates'),
                             'The model discovery module must exist')
        self.module = importlib.import_module('model_updates')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cache = self.home / 'private' / 'models.json'
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.module.Path, 'home', return_value=self.home).start()
        mock.patch.dict(os.environ, {'CCLOOP_MODEL_CACHE': str(self.cache),
                                    'CCLOOP_MODEL_METADATA_OFFLINE': ''}, clear=False).start()
        self.now = mock.patch.object(self.module.time, 'time', return_value=10000.0).start()
        self.fetch = mock.patch.object(self.module, '_fetch', side_effect=self.document).start()
        self.docs = {
            'claude': 'Fable is our most capable model: `claude-fable-5-1`. '
                      'Use `claude-sonnet-9-9` for fast daily work.',
            'codex': '<h2>Astra</h2><p>Our most capable model for complex work.</p>'
                     '<code>codex -m gpt-6-astra</code>'
                     '<h2>6.1 Sol</h2><p>Balanced speed and cost.</p>'
                     '<code>codex -m gpt-6.1-sol</code>',
            'kimi': '<pre>default_model = &quot;kimi-code/k3&quot;</pre>',
        }
        self.write_kimi({'kimi-code/k3': 'k3'})

    def document(self, url, timeout):
        for provider, source in self.module.SOURCES.items():
            if source == url:
                return self.docs[provider]
        self.fail('Unexpected metadata URL')

    def write_kimi(self, aliases):
        path = self.home / '.kimi' / 'config.toml'
        path.parent.mkdir(exist_ok=True)
        content = 'default_model = "kimi-code/k3"\n[providers.kimi]\n'
        content += 'type = "kimi"\napi_key = "secret-must-never-appear"\n'
        for alias, backend in aliases.items():
            content += f'[models."{alias}"]\nprovider = "kimi"\nmodel = "{backend}"\n'
            content += 'capabilities = ["thinking"]\n'
        path.write_text(content)

    def write_codex(self, entries):
        path = self.home / '.codex' / 'models_cache.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'models': entries}))

    def test_selects_capability_instead_of_larger_fast_model_version(self):
        result = self.module.resolve_models()
        self.assertEqual(result['models'], self.module.MODELS)
        self.assertTrue(result['refreshed'])
        self.assertEqual(result['checked_at'], 10000.0)
        for provider in self.module.MODELS:
            with self.subTest(provider=provider):
                if provider == 'kimi' and not HAS_TOMLLIB:
                    self.skipTest(TOMLLIB_REQUIREMENT)
                self.assertTrue(result['sources'][provider])

    def test_refreshes_new_frontier_versions_and_configured_kimi_alias(self):
        self.docs['claude'] = 'Our most capable: claude-fable-5-2. Older claude-fable-5-1.'
        self.docs['codex'] = 'Our most capable model: `gpt-7-astra`. Fast: `gpt-8-luna`.'
        self.docs['kimi'] = 'default_model = "kimi-code/k4"'
        self.write_kimi({'kimi-code/k3': 'k3', 'private-k4': 'k4'})
        result = self.module.resolve_models()
        expected = {'claude': 'claude-fable-5-2', 'codex': 'gpt-7-astra', 'kimi': 'private-k4'}
        for provider, model in expected.items():
            with self.subTest(provider=provider):
                if provider == 'kimi' and not HAS_TOMLLIB:
                    self.skipTest(TOMLLIB_REQUIREMENT)
                self.assertEqual(result['models'][provider], model)
        self.assertNotIn('secret-must-never-appear', json.dumps(result))
        self.assertNotIn('secret-must-never-appear', self.cache.read_text())

    def test_unconfigured_new_kimi_alias_is_not_invented(self):
        self.docs['kimi'] = 'default_model = "kimi-code/k4"'
        result = self.module.resolve_models()
        self.assertEqual(result['models']['kimi'], 'kimi-code/k3')
        self.assertTrue(any('configured' in warning for warning in result['warnings']))

    @unittest.skipUnless(HAS_TOMLLIB, TOMLLIB_REQUIREMENT)
    def test_newest_configured_kimi_frontier_is_used_when_official_next_is_missing(self):
        self.docs['kimi'] = 'default_model = "kimi-code/k5"'
        self.write_kimi({'kimi-code/k3': 'k3', 'private-k4': 'k4'})
        result = self.module.resolve_models()
        self.assertEqual(result['models']['kimi'], 'private-k4')
        self.assertTrue(any('configured' in warning for warning in result['warnings']))

    def test_without_tomllib_preserves_kimi_profile_and_reports_limitation(self):
        current = dict(self.module.MODELS, kimi='kimi-code/k4')
        self.docs['kimi'] = 'default_model = "kimi-code/k5"'
        self.write_kimi({'kimi-code/k5': 'k5'})
        with mock.patch.dict(sys.modules, {'tomllib': None}):
            result = self.module.resolve_models(current)
        self.assertEqual(result['models'], current)
        self.assertEqual(result['sources']['kimi'], [])
        self.assertTrue(any('Python 3.11+' in warning and 'retained' in warning
                            for warning in result['warnings']))
        self.assertNotIn('secret-must-never-appear', json.dumps(result))

    def test_offline_environment_never_fetches_metadata(self):
        with mock.patch.dict(os.environ, {'CCLOOP_MODEL_METADATA_OFFLINE': '1'}):
            result = self.module.resolve_models(force=True)
        self.assertEqual(result['models'], self.module.MODELS)
        self.assertFalse(result['refreshed'])
        self.assertFalse(self.cache.exists())
        self.fetch.assert_not_called()

    def test_cache_invalid_source_records_are_ignored(self):
        self.cache.parent.mkdir()
        self.cache.write_text(json.dumps({'models': self.module.MODELS,
                                         'checked_at': 10000, 'sources': {'kimi': None}}))
        result = self.module.resolve_models()
        self.assertEqual(result['models'], self.module.MODELS)
        self.assertTrue(result['warnings'])

    def test_cache_ttl_force_and_expiry(self):
        first = self.module.resolve_models()
        self.assertEqual(self.fetch.call_count, 3)
        self.now.return_value = 11799.0
        cached = self.module.resolve_models()
        self.assertFalse(cached['refreshed'])
        self.assertEqual(cached['checked_at'], first['checked_at'])
        self.assertEqual(self.fetch.call_count, 3)
        self.module.resolve_models(force=True)
        self.assertEqual(self.fetch.call_count, 6)
        self.now.return_value = 13600.0
        self.assertTrue(self.module.resolve_models()['refreshed'])
        self.assertEqual(self.fetch.call_count, 9)

    def test_offline_uses_cache_without_network_or_writes(self):
        empty = self.module.resolve_models(offline=True)
        self.assertEqual(empty['models'], self.module.MODELS)
        self.assertFalse(self.cache.exists())
        self.fetch.assert_not_called()
        self.docs['claude'] = 'claude-fable-5-2 is our most capable model.'
        self.module.resolve_models()
        contents = self.cache.read_bytes()
        self.fetch.reset_mock()
        self.now.return_value = 99999.0
        result = self.module.resolve_models(offline=True, force=True)
        self.assertEqual(result['models']['claude'], 'claude-fable-5-2')
        self.assertFalse(result['refreshed'])
        self.assertEqual(self.cache.read_bytes(), contents)
        self.fetch.assert_not_called()

    def test_stale_or_down_metadata_never_downgrades_known_frontier(self):
        current = {'claude': 'claude-fable-5-2', 'codex': 'gpt-7-astra', 'kimi': 'kimi-code/k3'}
        result = self.module.resolve_models(current)
        self.assertEqual(result['models'], current)
        self.assertTrue(any('older' in x or 'downgrade' in x for x in result['warnings']))
        self.fetch.side_effect = OSError('secret-must-never-appear')
        result = self.module.resolve_models(force=True)
        self.assertEqual(result['models'], current)
        self.assertNotIn('secret-must-never-appear', json.dumps(result))
        self.assertTrue(result['warnings'])

    def test_format_changes_retain_current_and_report_warning(self):
        self.docs = {p: '<html>documentation moved</html>' for p in self.module.MODELS}
        result = self.module.resolve_models()
        self.assertEqual(result['models'], self.module.MODELS)
        self.assertTrue(any('format' in x for x in result['warnings']))

    def test_local_codex_frontier_beats_fast_default_and_hidden_models(self):
        self.docs['codex'] = 'Documentation moved'
        self.write_codex([
            {'slug': 'gpt-8-luna', 'visibility': 'list', 'priority': 0,
             'description': 'Fast and affordable model'},
            {'slug': 'gpt-9-astra', 'visibility': 'hide', 'priority': 1,
             'description': 'Frontier intelligence'},
            {'slug': 'gpt-7-astra', 'visibility': 'list', 'priority': 2,
             'description': 'Frontier intelligence for the most demanding work'},
        ])
        result = self.module.resolve_models()
        self.assertEqual(result['models']['codex'], 'gpt-7-astra')
        self.assertIn('models_cache.json', json.dumps(result['sources']['codex']))

    def test_official_strongest_statement_can_introduce_new_model_family(self):
        self.docs['codex'] = 'Our most capable model is `gpt-7-zenith`.\n\n'
        self.docs['codex'] += 'Our fastest model is `gpt-8-spark`.'
        self.assertEqual(self.module.resolve_models()['models']['codex'], 'gpt-7-zenith')

    def test_current_newer_than_fresh_cache_is_preserved(self):
        self.module.resolve_models()
        result = self.module.resolve_models({'claude': 'claude-fable-6-1'})
        self.assertEqual(result['models']['claude'], 'claude-fable-6-1')
        self.assertEqual(self.fetch.call_count, 3)

    def test_cache_is_private_and_atomic_replace_failure_keeps_previous_file(self):
        self.module.resolve_models()
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.cache.parent.stat().st_mode), 0o700)
        before = self.cache.read_bytes()
        self.docs['claude'] = 'Our most capable model: claude-fable-5-2'
        with mock.patch.object(self.module.os, 'replace', side_effect=OSError('failed')):
            result = self.module.resolve_models(force=True)
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertTrue(any('cache' in x for x in result['warnings']))
        self.assertEqual([p.name for p in self.cache.parent.iterdir()], ['models.json'])

    def test_corrupt_cache_and_invalid_kimi_config_do_not_expose_contents(self):
        self.cache.parent.mkdir()
        self.cache.write_text('{secret-must-never-appear')
        (self.home / '.kimi/config.toml').write_text('secret-must-never-appear= [')
        result = self.module.resolve_models()
        self.assertNotIn('secret-must-never-appear', json.dumps(result))
        self.assertEqual(result['models']['kimi'], self.module.MODELS['kimi'])
        self.assertTrue(result['warnings'])

    def test_fetch_is_bounded_and_sets_public_only_headers(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'public metadata'
        # Exercise the HTTP implementation, below the fixture fetch seam.
        with mock.patch.object(self.module.urllib.request, 'urlopen', return_value=response) as call:
            self.module._http_fetch(self.module.SOURCES['claude'], 5)
        request = call.call_args.args[0]
        self.assertTrue(request.get_header('User-agent'))
        self.assertIsNone(request.get_header('Authorization'))
        self.assertLessEqual(call.call_args.kwargs['timeout'], 5)

    def test_slow_metadata_does_not_block_resolver_indefinitely(self):
        def slow(url, timeout):
            time.sleep(0.5)
            return 'unused'
        self.fetch.side_effect = slow
        with mock.patch.object(self.module, 'FETCH_TIMEOUT', 0.03), \
                mock.patch.object(self.module, '_write_cache'):
            started = time.monotonic()
            result = self.module.resolve_models()
            duration = time.monotonic() - started
        self.assertLess(duration, 0.3)
        self.assertEqual(result['models'], self.module.MODELS)
        self.assertTrue(any('timeout' in x.lower() for x in result['warnings']))


if __name__ == '__main__':
    unittest.main()

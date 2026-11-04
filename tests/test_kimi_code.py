"""Kimi Code reviewer contracts exercised through an actual subprocess."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    with patch.object(sys, 'path', [str(SCRIPTS), *sys.path]):
        spec.loader.exec_module(result)
    return result


FAKE = r'''import json, os, pathlib, sys, time
try:
    import tomllib
except ImportError:
    import tomli as tomllib
args = sys.argv[1:]
base = pathlib.Path(os.environ['CCLOOP_KIMI_TEST_BASE'])
if '--prompt' not in args:
    print('unsupported legacy invocation', file=sys.stderr)
    sys.exit(2)
for flag in ('--print', '--input-format', '--final-message-only', '--thinking', '--mcp-config-file', '--work-dir', '--max-steps-per-turn', '--max-ralph-iterations', '--auto', '--yolo'):
    if flag in args:
        print('unsupported or unsafe flag: ' + flag, file=sys.stderr)
        sys.exit(2)
assert args[args.index('--output-format') + 1] == 'stream-json'
agent = pathlib.Path(args[args.index('--agent-file') + 1]).read_text()
assert agent.startswith('---\n') and '\ndescription:' in agent
assert '\ntools: []\n' in agent and '\nsubagents: []\n' in agent
assert '${' not in agent
packet = json.loads(agent.split('\n```json\n', 1)[1].split('\n```', 1)[0])
home = pathlib.Path(os.environ['KIMI_CODE_HOME'])
config = tomllib.loads((home / 'config.toml').read_text())
if not config.get('default_model'):
    print('error: failed to run prompt: no default model configured', file=sys.stderr)
    sys.exit(1)
entry = {'args': args, 'packet': packet, 'home': str(home), 'cwd': os.getcwd(),
         'agent_in_home': pathlib.Path(args[args.index('--agent-file') + 1]).is_relative_to(home),
         'skills_in_home': pathlib.Path(args[args.index('--skills-dir') + 1]).is_relative_to(home),
         'cwd_in_home': pathlib.Path.cwd().is_relative_to(home),
         'payload_in_home': (home / 'stdin.json').is_file(),
         'config': config, 'home_mode': home.stat().st_mode & 511,
         'config_mode': (home / 'config.toml').stat().st_mode & 511,
         'home_entries': sorted(p.name for p in home.iterdir()),
         'credential_target': str((home / 'credentials').resolve()) if (home / 'credentials').exists() else None,
         'skills': list(pathlib.Path(args[args.index('--skills-dir') + 1]).iterdir()),
         'model_env': os.environ.get('KIMI_MODEL_NAME'),
         'key_available': os.environ.get('TEST_PROVIDER_KEY') == 'fixture-key'}
(base / 'call.json').write_text(json.dumps(entry))
mode = os.environ.get('CCLOOP_KIMI_TEST_MODE', 'ok')
print(json.dumps({'role': 'meta', 'type': 'system.version', 'version': '2.4.1'}))
print(json.dumps({'type': 'thinking.delta', 'delta': 'PRIVATE_REASONING_STDOUT'}))
print('PRIVATE_REASONING_STDERR', file=sys.stderr)
if mode == 'wait':
    (base / 'started').touch()
    time.sleep(20)
if mode == 'auth':
    print(json.dumps({'type': 'error', 'code': 'AUTHENTICATION_REQUIRED', 'message': 'Please log in with kimi login', 'thinking': 'PRIVATE_ERROR_THOUGHT'}))
    sys.exit(0)
if mode == 'auth_stderr':
    print('Error: HTTP 401 unauthorized; run kimi login', file=sys.stderr)
    sys.exit(1)
if mode == 'auth_code':
    print('error: failed to run prompt: auth.token_missing: OAuth token provider is not configured. Run kimi login.', file=sys.stderr)
    sys.exit(1)
if mode == 'auth_block':
    print('error: Request failed\n\nmessage:\nAuthentication required; run kimi login\nSee log: /private/kimi.log', file=sys.stderr)
    sys.exit(1)
if mode == 'invalid':
    print('PRIVATE_INVALID_UNFRAMED_PAYLOAD')
    sys.exit(0)
if mode == 'tool':
    print(json.dumps({'role': 'assistant', 'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'Bash', 'arguments': '{}'}}]}))
report = {'summary': 'Checked supplied evidence', 'received': packet}
if mode == 'readiness':
    assert packet == {'request': 'Return only the JSON object {"status":"ready"}.'}
    report = {'status': 'ready'}
message = {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'PRIVATE_ASSISTANT_THOUGHT'}, {'type': 'text', 'text': json.dumps(report)}]}
if mode == 'observed':
    message['model'] = 'k3-observed'
if mode == 'configured':
    message['model'] = args[args.index('--model') + 1]
print(json.dumps(message))
print(json.dumps({'role': 'meta', 'type': 'session.resume_hint', 'session_id': 'private-session', 'content': 'kimi -r private-session'}))
sys.exit(1 if mode == 'nonzero' else 0)
'''


CONFIG = '''default_model = "old-default"
default_permission_mode = "auto"
extra_agent_dirs = ["/do/not/load"]
extra_skill_dirs = ["/do/not/load"]
[[hooks]]
event = "SessionStart"
command = "touch forbidden"
[thinking]
enabled = false
effort = "low"
[providers."managed:kimi-code"]
type = "kimi"
base_url = "https://api.kimi.com/coding/v1"
api_key_env = "TEST_PROVIDER_KEY"
default_model = "k3"
[providers."managed:kimi-code".custom_headers]
"X-Quoted.Name" = "a\\tb\\nquoted \\"value\\""
[providers."managed:kimi-code".source]
type = "custom"
version = 3
entries = [{name = "one", enabled = true, weight = 1.25}]
[models."kimi-code/k3"]
provider = "managed:kimi-code"
model = "k3"
max_context_size = 1048576
max_input_size = 1000000
max_output_size = 32768
capabilities = ["thinking", "tool_use"]
support_efforts = ["low", "high", "max"]
default_effort = "high"
adaptive_thinking = true
beta_api = false
protocol = "anthropic"
base_url = "https://api.kimi.com/coding/anthropic"
display_name = "K3 한글"
[models."kimi-code/k3".overrides]
max_context_size = 1048576
default_effort = "max"
[models."kimi-code/k3".upgrade]
model = "k3-new"
migration_markdown = "Future model\\nDo not use yet"
retirement_at = "2027-01-01"
'''


class CodeReviewerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.code_home = self.home / '.kimi-code'
        self.code_home.mkdir()
        (self.code_home / 'config.toml').write_text(CONFIG)
        (self.code_home / 'mcp.json').write_text('{"mcpServers":{"unsafe":{}}}')
        (self.code_home / 'plugins').mkdir()
        (self.code_home / 'plugins' / 'installed.json').write_text('{}')
        (self.code_home / 'credentials').mkdir()
        (self.code_home / 'credentials' / 'kimi-code.json').write_text('{"fixture":true}')
        legacy = self.home / '.kimi'
        legacy.mkdir()
        (legacy / 'config.toml').write_text('[hooks]\nPreToolUse = [{command="forbidden"}]\n')
        self.fake = self.base / 'kimi-code'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE)
        self.fake.chmod(0o700)
        self.environment = dict(os.environ, HOME=str(self.home),
            CCLOOP_KIMI_TEST_BASE=str(self.base), KIMI_MODEL_NAME='wrong-model',
            TEST_PROVIDER_KEY='fixture-key')
        self.environment.pop('CCLOOP_KIMI_FLAVOR', None)
        self.environment.pop('KIMI_CODE_HOME', None)
        self.config = {'commands': {'kimi': [str(self.fake)]}, 'models': {'kimi': 'kimi-code/k3'}}
        self.providers = module('providers')

    def invoke(self, mode='ok', prompt=None, cancelled=lambda: False, timeout=3):
        with patch.dict(os.environ, dict(self.environment, CCLOOP_KIMI_TEST_MODE=mode), clear=True):
            return self.providers.invoke('kimi', self.config,
                json.dumps({'evidence': '한글 ${base_prompt}'} if prompt is None else prompt),
                self.base / 'audit', timeout, cancelled, lambda attempt: True)

    def call(self):
        return json.loads((self.base / 'call.json').read_text())

    def test_code_reviewer_uses_documented_flags_and_complete_private_configuration(self):
        attempt, report = self.invoke()
        self.assertEqual(attempt['status'], 'received', attempt)
        self.assertEqual(report['received'], {'evidence': '한글 ${base_prompt}'})
        call = self.call()
        self.assertEqual(call['config']['thinking'], {'enabled': True, 'effort': 'max'})
        self.assertEqual(set(call['config']) & {'hooks', 'extra_agent_dirs', 'extra_skill_dirs', 'default_permission_mode'}, set())
        self.assertNotIn('mcp.json', call['home_entries'])
        self.assertNotIn('plugins', call['home_entries'])
        self.assertEqual(call['home_mode'], 0o700)
        self.assertEqual(call['config_mode'], 0o600)
        for field in ('agent_in_home', 'skills_in_home', 'cwd_in_home', 'payload_in_home'):
            self.assertTrue(call[field], field + ' must share the container home bind')
        self.assertEqual(call['credential_target'], str(self.code_home / 'credentials'))
        self.assertEqual(call['skills'], [])
        self.assertIsNone(call['model_env'])
        self.assertTrue(call['key_available'])
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        original = tomllib.loads(CONFIG)
        self.assertEqual(call['config']['providers'], original['providers'])
        self.assertEqual(call['config']['models'], original['models'])
        self.assertEqual((self.code_home / 'config.toml').read_text(), CONFIG)
        self.assertFalse(Path(call['home']).exists(), 'Per-call home must be removed')
        self.assertEqual(attempt['requested_model'], 'kimi-code/k3')
        self.assertEqual(attempt['observed_models'], [])

    def test_large_packet_is_complete_without_argv_limits_or_template_expansion(self):
        packet = {'evidence': '한글 ${base_prompt} evidence\n' * 20000}
        attempt, report = self.invoke(prompt=packet)
        self.assertEqual(attempt['status'], 'received', attempt)
        self.assertEqual(report['received'], packet)
        self.assertLess(sum(len(arg) for arg in self.call()['args']), 5000)

    def test_requested_model_is_also_the_private_startup_default(self):
        attempt, report = self.invoke()
        self.assertEqual(attempt['status'], 'received', attempt)
        self.assertEqual(self.call()['config']['default_model'], 'kimi-code/k3')
        self.assertEqual((self.code_home / 'config.toml').read_text(), CONFIG)
        self.assertIsNotNone(report)

    def test_unicode_model_metadata_survives_toml_serialization(self):
        original = CONFIG.replace('K3 한글', 'K3 한글 🚀 \\u007f')
        (self.code_home / 'config.toml').write_text(original)
        attempt, report = self.invoke()
        self.assertEqual(attempt['status'], 'received', attempt)
        self.assertEqual(self.call()['config']['models']['kimi-code/k3']['display_name'], 'K3 한글 🚀 \x7f')
        self.assertIsNotNone(report)

    def test_plain_text_readiness_prompt_reaches_the_tool_free_agent(self):
        with patch.dict(os.environ, dict(self.environment, CCLOOP_KIMI_TEST_MODE='readiness'), clear=True):
            attempt, report = self.providers.invoke('kimi', self.config,
                'Return only the JSON object {"status":"ready"}.', self.base / 'audit',
                3, lambda: False, lambda details: True)
        self.assertEqual(attempt['status'], 'received', attempt)
        self.assertEqual(report, {'status': 'ready'})

    def test_audit_contains_external_answer_only_without_claiming_model_attestation(self):
        attempt, report = self.invoke('observed')
        self.assertEqual(attempt['status'], 'received', attempt)
        self.assertEqual(attempt['observed_models'], [])
        audit = ''.join(p.read_text() for p in (self.base / 'audit').iterdir())
        self.assertNotIn('PRIVATE_', audit)
        self.assertNotIn('private-session', audit)
        self.assertIn('Checked supplied evidence', audit)
        self.assertEqual(report['summary'], 'Checked supplied evidence')

    def test_configured_model_field_is_not_a_served_model_attestation(self):
        attempt, report = self.invoke('configured')
        self.assertEqual(attempt['status'], 'received', attempt)
        self.assertEqual(attempt['requested_model'], 'kimi-code/k3')
        self.assertEqual(attempt['observed_models'], [])
        self.assertIsNotNone(report)

    def test_native_auth_errors_are_access_failures_without_private_fields(self):
        for mode in ('auth', 'auth_stderr', 'auth_code', 'auth_block'):
            with self.subTest(mode=mode):
                attempt, report = self.invoke(mode)
                self.assertEqual(attempt['status'], 'access_error', attempt)
                self.assertIsNone(report)
                self.assertIn('login', attempt['error'])
                audit = json.dumps(attempt) + ''.join(p.read_text() for p in (self.base / 'audit').iterdir())
                self.assertNotIn('PRIVATE_', audit)

    def test_tool_events_fail_closed_even_when_followed_by_a_valid_report(self):
        attempt, report = self.invoke('tool')
        self.assertTrue((self.base / 'call.json').exists(), attempt)
        self.assertEqual(attempt['failure_kind'], 'unsafe', attempt)
        self.assertIsNone(report)

    def test_invalid_stream_and_nonzero_report_never_become_access_failure(self):
        for mode, expected in [('invalid', 'invalid'), ('nonzero', 'provider')]:
            with self.subTest(mode=mode):
                attempt, report = self.invoke(mode)
                self.assertEqual(attempt['failure_kind'], expected, attempt)
                self.assertIsNone(report)
                audit = json.dumps(attempt) + ''.join(p.read_text() for p in (self.base / 'audit').iterdir())
                self.assertNotIn('PRIVATE_', audit)

    def test_cancellation_does_not_retain_private_streams(self):
        attempt, report = self.invoke('wait', cancelled=lambda: (self.base / 'started').exists())
        self.assertEqual(attempt['status'], 'cancelled', attempt)
        self.assertIsNone(report)
        audit = ''.join(p.read_text() for p in (self.base / 'audit').iterdir())
        self.assertNotIn('PRIVATE_', audit)

    def test_doctor_reports_flavor_and_config_path_without_models_or_secrets(self):
        with patch.dict(os.environ, self.environment, clear=True):
            result = self.providers.doctor(self.config)['providers']['kimi']
        self.assertEqual(result.get('cli_kind'), 'code', result)
        self.assertEqual(result.get('config_source'), str(self.code_home / 'config.toml'))
        self.assertNotIn('preflight_error', result)
        self.assertNotIn('observed_models', result)
        self.assertFalse((self.base / 'call.json').exists())

    def test_default_command_uses_the_shared_client_resolver(self):
        self.config.pop('commands')
        with patch.object(self.providers.runtime_paths, 'client', return_value=(str(self.fake), str(self.fake)), create=True):
            attempt, report = self.invoke()
            with patch.dict(os.environ, self.environment, clear=True):
                info = self.providers.doctor(self.config)['providers']['kimi']
        self.assertEqual(attempt['status'], 'received', attempt)
        self.assertIsNotNone(report)
        self.assertEqual(info['executable'], str(self.fake))

    def test_bare_kimi_command_uses_the_shared_client_resolver(self):
        self.config['commands']['kimi'] = ['kimi']
        with patch.object(self.providers.runtime_paths, 'client', return_value=(str(self.fake), str(self.fake)), create=True):
            attempt, report = self.invoke()
        self.assertEqual(attempt['status'], 'received', attempt)
        self.assertIsNotNone(report)


class RuntimeDetectionTest(unittest.TestCase):
    def runtime(self):
        self.assertTrue((SCRIPTS / 'kimi_runtime.py').exists(), 'Shared Kimi runtime detection is missing')
        return module('kimi_runtime')

    def test_detection_does_not_launch_unknown_or_legacy_commands(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as name, patch.dict(os.environ, {}, clear=True):
            root = Path(name)
            fake = root / 'kimi'
            sentinel = root / 'launched'
            fake.write_text('#!/bin/sh\ntouch ' + str(sentinel) + '\n')
            fake.chmod(0o700)
            self.assertEqual(runtime.kind(str(fake)), 'python')
            self.assertEqual(runtime.kind(str(root / 'kimi-code')), 'code')
            self.assertEqual(runtime.kind(str(root / '.kimi-code/bin/kimi')), 'code')
            self.assertEqual(runtime.kind(str(root / '.local/share/ccloop/launchers/kimi-code-direct')), 'code')
            package = root / 'node_modules/@moonshot-ai/kimi-code'
            (package / 'dist').mkdir(parents=True)
            (package / 'package.json').write_text('{"name":"@moonshot-ai/kimi-code","version":"2.4.1"}')
            entry = package / 'dist/main.mjs'
            entry.touch()
            self.assertEqual(runtime.kind(str(entry)), 'code')
            self.assertFalse(sentinel.exists())

    def test_explicit_flavor_and_code_home_overrides_are_honored(self):
        runtime = self.runtime()
        with patch.dict(os.environ, {'CCLOOP_KIMI_FLAVOR': 'code', 'KIMI_CODE_HOME': '/private/code'}, clear=True):
            self.assertEqual(runtime.kind('/custom/wrapper'), 'code')
            self.assertEqual(runtime.code_home(), Path('/private/code'))
        with patch.dict(os.environ, {'CCLOOP_KIMI_FLAVOR': 'python'}, clear=True):
            self.assertEqual(runtime.kind('/custom/kimi-code'), 'python')
        with patch.dict(os.environ, {'CCLOOP_KIMI_FLAVOR': 'typo'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'CCLOOP_KIMI_FLAVOR'):
                runtime.kind('/custom/wrapper')

    def test_missing_toml_backend_fails_before_launch_with_honest_python_requirement(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / 'source/config.toml'
            config.parent.mkdir()
            config.write_text('[providers]\n')
            work = root / 'work'
            work.mkdir()
            with patch.dict(os.environ, {'KIMI_CODE_HOME': str(config.parent)}), patch.dict(sys.modules, {'tomllib': None, 'tomli': None}):
                with self.assertRaisesRegex(ValueError, 'Python 3.11'):
                    runtime.reviewer_command(['kimi-code'], {'models': {'kimi': 'kimi-code/k3'}}, work)

    def test_python310_can_use_optional_tomli_without_changing_config_safety(self):
        runtime = self.runtime()
        try:
            import tomllib as backend
        except ImportError:
            import tomli as backend
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            original = root / 'original'
            original.mkdir()
            (original / 'config.toml').write_text(CONFIG)
            work = root / 'work'
            work.mkdir()
            with patch.dict(os.environ, {'KIMI_CODE_HOME': str(original)}), patch.dict(sys.modules, {'tomllib': None, 'tomli': backend}):
                try:
                    argv = runtime.reviewer_command(['kimi-code'], {'models': {'kimi': 'kimi-code/k3'}}, work)
                except ValueError as error:
                    self.fail('The available tomli backend was not used: ' + str(error))
            result = backend.loads((work / 'config.toml').read_text())
            self.assertIn('--prompt', argv)
            self.assertEqual(result['models'], backend.loads(CONFIG)['models'])
            self.assertNotIn('hooks', result)


if __name__ == '__main__':
    unittest.main()

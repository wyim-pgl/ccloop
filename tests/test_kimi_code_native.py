"""Modern Kimi ACP at the real child-process, permission and ledger boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import checkpoints
import control


# Shapes are from Kimi Code CLI 2.4.1's ACP SDK and official ACP reference.
# This child never imports a model SDK, reads credentials or accesses a network.
ACP = r'''#!/usr/bin/env python3
import json,os,pathlib,sys,time
base=pathlib.Path(os.environ['ACP_FIXTURE'])
settings=json.loads((base/'behavior.json').read_text())
sid='acp-session'
model='configured-default'
level='off'
mode='auto'
pending=None
repeated=False
def log(value):
    with (base/'calls.jsonl').open('a') as stream:stream.write(json.dumps(value)+'\n')
def emit(value):print(json.dumps(value),flush=True)
def reply(msg,result):emit(dict(jsonrpc='2.0',id=msg['id'],result=result))
def update(value):emit(dict(jsonrpc='2.0',method='session/update',params=dict(sessionId=sid,update=value)))
def options():
    if settings.get('no_config'):return []
    result=[dict(id='model',name='Model',type='select',category='model',currentValue=model,options=[dict(value=model,name=model)])]
    if model!='configured-default':
        result.append(dict(id='thinking',name='Thinking',type='select',category='thought_level',currentValue=level,options=[dict(value=v,name=v.title()) for v in settings.get('efforts',['off','on','high','max'])]))
    result.append(dict(id='mode',name='Mode',type='select',category='mode',currentValue=mode,options=[dict(value=v,name=v.title()) for v in ['default','auto','yolo']]))
    return result
def finish(msg):
    if settings.get('preface'):
        update(dict(sessionUpdate='agent_message_chunk',content=dict(type='text',text='I will inspect the artifact before reporting.')))
    update(dict(sessionUpdate='agent_thought_chunk',content=dict(type='text',text='PRIVATE_ACP_THOUGHT')))
    update(dict(sessionUpdate='tool_call',toolCallId='read-1',title='Read artifact',kind='read',status='in_progress',rawInput=dict(path='artifact.txt')))
    update(dict(sessionUpdate='tool_call_update',toolCallId='read-1',status='completed',content=[dict(type='content',content=dict(type='text',text='VISIBLE_TOOL_FACT'))],rawOutput='initial'))
    report=dict(binding=json.loads(msg['params']['prompt'][0]['text'])['binding'],summary='ACP completed',status='complete')
    text=json.dumps(report)
    if settings.get('fence'):text='```json\n'+text+'\n```'
    for part in [text[:20],text[20:]]:update(dict(sessionUpdate='agent_message_chunk',content=dict(type='text',text=part)))
    reply(msg,dict(stopReason=settings.get('stop_reason','end_turn')))
code_home=pathlib.Path(os.environ['KIMI_CODE_HOME'])
marker=code_home/'config-marker'
log(dict(argv=sys.argv[1:],config_marker=marker.read_text() if marker.exists() else None))
if sys.argv[1:]!=['acp']:
    print('Modern client requires the acp subcommand',file=sys.stderr);sys.exit(2)
for line in sys.stdin:
    msg=json.loads(line);log(msg)
    method=msg.get('method');params=msg.get('params',{})
    if 'id' in msg and method is None:
        if pending and msg.get('result',{}).get('outcome',{}).get('optionId')=='once':
            if settings.get('repeat_permission') and not repeated:
                repeated=True
                permission_request['params']['toolCall']['toolCallId']='edit-2'
                emit(permission_request)
                continue
            finish(pending)
        pending=None
        continue
    if settings.get('error_method')==method:
        emit(dict(jsonrpc='2.0',id=msg['id'],error=settings['rpc_error']));continue
    if settings.get('result_method')==method:
        reply(msg,settings.get('result_value'));continue
    if method=='initialize':
        reply(msg,dict(protocolVersion=settings.get('protocol',1),agentCapabilities=dict(loadSession=True),agentInfo=dict(name='Kimi Code CLI',version='2.4.1'),authMethods=[]))
    elif method=='session/new':
        reply(msg,dict(sessionId=sid,configOptions=options(),modes=None if settings.get('no_modes') else dict(currentModeId=mode,availableModes=[dict(id=v,name=v.title()) for v in ['default','auto','yolo']])))
    elif method=='session/set_model':
        model=settings.get('selected_model',params['modelId'])
        update(dict(sessionUpdate='config_option_update',configOptions=options()))
        reply(msg,{})
    elif method=='session/set_mode':
        mode=params['modeId'];update(dict(sessionUpdate='config_option_update',configOptions=options()));reply(msg,{})
    elif method=='session/set_config_option':
        assert params['configId']=='thinking'
        assert params['value'] in settings.get('efforts',['off','on','high','max'])
        level=params['value'];reply(msg,dict(configOptions=options()))
    elif method=='session/prompt':
        if 'update_value' in settings:update(settings['update_value'])
        if 'notification_params' in settings:
            emit(dict(jsonrpc='2.0',method='session/update',params=settings['notification_params']))
        if 'reverse_request' in settings:emit(settings['reverse_request'])
        if settings.get('permission'):
            pending=msg
            opts=[dict(optionId='always',name='Always allow',kind='allow_always'),dict(optionId='once',name='Allow once',kind='allow_once')]
            if settings.get('permission')=='no_once':opts=opts[:1]
            if settings.get('permission')!='no_reject':opts.append(dict(optionId='deny',name='Reject',kind='reject_once'))
            permission_request=dict(jsonrpc='2.0',id='permission-1',method='session/request_permission',params=dict(sessionId=sid,toolCall=dict(toolCallId='edit-1',title='Write artifact',kind='edit',status='pending',rawInput=dict(path='artifact.txt')),options=opts))
            if 'permission_params' in settings:permission_request['params']=settings['permission_params']
            emit(permission_request)
        elif settings.get('wait'):
            pending=msg;(base/'ready').write_text('ready')
        else:finish(msg)
    elif method=='session/cancel':
        if pending:reply(pending,dict(stopReason='cancelled'))
        pending=None
    else:
        emit(dict(jsonrpc='2.0',id=msg.get('id'),error=dict(code=-32601,message='Unknown method')))
if settings.get('late_thought'):
    update(dict(sessionUpdate='agent_thought_chunk',content=dict(type='text',text='PRIVATE_ACP_THOUGHT')))
sys.exit(settings.get('exit_code',0))
'''


class KimiCodeNativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        (self.project / 'artifact.txt').write_text('initial')
        self.root = self.base / 'ledger'
        self.root.mkdir()
        self.directory = self.root / 'turn'
        self.directory.mkdir()
        self.executable = self.base / 'kimi-code'
        self.executable.write_text(ACP)
        self.executable.chmod(0o700)
        self.state = dict(enabled=True, run_id='acp-run', generation=2, revision=3,
                          deadline=None, config=dict(project=str(self.project), watch=['*.txt']))
        control.atomic_json(self.root / 'state.json', self.state)
        snapshot = checkpoints.safe_snapshot(self.state)
        self.binding = dict(run_id='acp-run', generation=2, artifact_sha256=snapshot['sha256'])
        self.job = dict(root=str(self.root), driver='kimi', executable=str(self.executable),
                        project=str(self.project), timeout_seconds=8, access_timeout=4,
                        model='kimi-code/k3', binding=self.binding, expected_revision=3,
                        artifact_snapshot=snapshot, prompt=json.dumps(dict(binding=self.binding)),
                        report_schema=dict(type='object'), session_id='old-session-must-not-replay')
        control.atomic_json(self.directory / 'job.json', self.job)
        self.environment = dict(os.environ, ACP_FIXTURE=str(self.base),
                                KIMI_CODE_HOME=str(self.base / 'code-home'), CCLOOP_KIMI_FLAVOR='code')
        self.settings()
        self.owner_write = None

    def settings(self, **kwargs):
        control.atomic_json(self.base / 'behavior.json', kwargs)

    def start(self):
        owner_read, self.owner_write = os.pipe()
        try:
            process = subprocess.Popen([sys.executable, str(SCRIPTS / 'native_driver.py'),
                str(self.directory / 'job.json'), str(owner_read)], pass_fds=(owner_read,),
                env=self.environment, cwd=self.base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        finally:
            os.close(owner_read)
        self.addCleanup(lambda: self.cleanup(process))
        return process

    def cleanup(self, process):
        self.close_owner()
        if process.poll() is None:
            process.kill()
        process.communicate()

    def close_owner(self):
        if self.owner_write is not None:
            os.close(self.owner_write)
            self.owner_write = None

    def result(self, process):
        stdout, stderr = process.communicate(timeout=12)
        self.assertEqual(process.returncode, 0, stderr + stdout)
        return json.loads((self.directory / 'result.json').read_text())

    def calls(self):
        path = self.base / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, path, process):
        end = time.monotonic() + 6
        while time.monotonic() < end:
            if path.exists():
                return
            if process.poll() is not None:
                self.fail('Supervisor ended before fixture signal: ' + str(self.result(process)))
            time.sleep(.02)
        self.fail('Timed out waiting for fixture signal: ' + str(path))

    def answer(self, approve, **settings):
        self.settings(permission=True, **settings)
        process = self.start()
        self.wait_for(self.directory / 'approval.json', process)
        request = json.loads((self.directory / 'approval.json').read_text())
        self.assertEqual(request['binding'], self.binding)
        self.assertEqual(request['native']['method'], 'session/request_permission')
        control.atomic_json(self.directory / 'approval-answer.json', dict(id=request['id'], approve=approve))
        return self.result(process)

    def permission_reply(self):
        return next(call for call in self.calls() if call.get('id') == 'permission-1' and 'result' in call)

    def test_acp_selects_model_and_max_thinking_without_fabricating_observed_model(self):
        self.settings(late_thought=True)
        result = self.result(self.start())
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['report']['binding'], self.binding)
        self.assertEqual(result['binding'], self.binding)
        self.assertEqual(result['requested_model'], 'kimi-code/k3')
        self.assertEqual(result['selected_model'], 'kimi-code/k3')
        self.assertEqual(result['observed_models'], [])
        self.assertEqual(result['native_returncode'], 0)
        calls = self.calls()
        self.assertEqual(calls[0]['argv'], ['acp'])
        initialize = next(call['params'] for call in calls if call.get('method') == 'initialize')
        self.assertEqual(initialize['protocolVersion'], 1)
        self.assertFalse(initialize['clientCapabilities'].get('terminal'))
        self.assertFalse(any(initialize['clientCapabilities'].get('fs', {}).values()))
        new = next(call['params'] for call in calls if call.get('method') == 'session/new')
        self.assertEqual(new, dict(cwd=str(self.project), mcpServers=[]))
        self.assertFalse(any(call.get('method') in {'session/load', 'session/resume'} for call in calls))
        select = next(call['params'] for call in calls if call.get('method') == 'session/set_model')
        self.assertEqual(select, dict(sessionId='acp-session', modelId='kimi-code/k3'))
        thinking = next(call['params'] for call in calls if call.get('method') == 'session/set_config_option')
        self.assertEqual(thinking, dict(sessionId='acp-session', configId='thinking', value='max'))
        normal = next(call['params'] for call in calls if call.get('method') == 'session/set_mode')
        self.assertEqual(normal['modeId'], 'default')
        prompt = next(call['params'] for call in calls if call.get('method') == 'session/prompt')
        self.assertEqual(prompt, dict(sessionId='acp-session', prompt=[dict(type='text', text=self.job['prompt'])]))
        self.assertIn('VISIBLE_TOOL_FACT', json.dumps(result['native_events']))
        for path in self.directory.iterdir():
            if path.is_file():
                self.assertNotIn('PRIVATE_ACP_THOUGHT', path.read_text(errors='replace'), str(path))

    def test_config_selection_is_unknown_without_server_evidence(self):
        self.settings(no_config=True)
        result = self.result(self.start())
        self.assertEqual(result['status'], 'completed', result)
        self.assertIsNone(result.get('selected_model'))
        self.assertEqual(result['observed_models'], [])
        self.assertFalse(any(call.get('method') == 'session/set_config_option' for call in self.calls()))

    def test_relative_code_home_keeps_parent_config_root_after_child_changes_cwd(self):
        source = self.base / 'relative-code-home'
        source.mkdir()
        (source / 'config-marker').write_text('chosen code config')
        self.environment['KIMI_CODE_HOME'] = 'relative-code-home'
        result = self.result(self.start())
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(self.calls()[0]['config_marker'], 'chosen code config')
        self.assertEqual(result['config_source'], str(source / 'config.toml'))

    def test_tool_narration_does_not_corrupt_final_json_report(self):
        self.settings(preface=True, fence=True)
        result = self.result(self.start())
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['report']['summary'], 'ACP completed')
        self.assertIn('I will inspect the artifact', (self.directory / 'stdout.jsonl').read_text())

    def test_manual_mode_is_explicit_even_without_optional_modes_metadata(self):
        self.settings(no_modes=True)
        result = self.result(self.start())
        self.assertEqual(result['status'], 'completed', result)
        selections = [call['params']['modeId'] for call in self.calls()
                      if call.get('method') == 'session/set_mode']
        self.assertEqual(selections, ['default'])

    def test_older_thinking_toggle_uses_only_advertised_on_value(self):
        self.settings(efforts=['off', 'on'])
        result = self.result(self.start())
        self.assertEqual(result['status'], 'completed', result)
        thinking = next(call['params'] for call in self.calls() if call.get('method') == 'session/set_config_option')
        self.assertEqual(thinking['value'], 'on')

    def test_selected_model_mismatch_stops_before_work(self):
        self.settings(selected_model='other-model')
        result = self.result(self.start())
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['failure_kind'], 'access')
        self.assertFalse(result['work_started'])
        self.assertEqual(result['observed_models'], [])
        self.assertFalse(any(call.get('method') == 'session/prompt' for call in self.calls()))

    def test_permission_denial_sends_selected_reject_and_cannot_complete(self):
        result = self.answer(False)
        self.assertEqual(result['failure_kind'], 'permission', result)
        self.assertEqual(self.permission_reply()['result'], dict(outcome=dict(outcome='selected', optionId='deny')))

    def test_permission_approval_grants_only_this_operation(self):
        result = self.answer(True)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(self.permission_reply()['result'], dict(outcome=dict(outcome='selected', optionId='once')))

    def test_reused_permission_id_cannot_reuse_prior_human_approval(self):
        result = self.answer(True, repeat_permission=True)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['failure_kind'], 'permission')
        replies = [call['result'] for call in self.calls()
                   if call.get('id') == 'permission-1' and 'result' in call]
        self.assertEqual(replies, [dict(outcome=dict(outcome='selected', optionId='once')),
                                   dict(outcome=dict(outcome='cancelled'))])

    def test_permission_without_reject_option_returns_cancelled(self):
        self.settings(permission='no_reject')
        process = self.start()
        self.wait_for(self.directory / 'approval.json', process)
        control.atomic_json(self.directory / 'approval-answer.json', dict(id='permission-1', approve=False))
        result = self.result(process)
        self.assertEqual(result['failure_kind'], 'permission', result)
        self.assertEqual(self.permission_reply()['result'], dict(outcome=dict(outcome='cancelled')))

    def test_per_operation_approval_cannot_become_always_allow(self):
        self.settings(permission='no_once')
        process = self.start()
        self.wait_for(self.directory / 'approval.json', process)
        control.atomic_json(self.directory / 'approval-answer.json', dict(id='permission-1', approve=True))
        result = self.result(process)
        self.assertEqual(result['failure_kind'], 'permission', result)
        self.assertEqual(self.permission_reply()['result'], dict(outcome=dict(outcome='cancelled')))

    def test_auth_required_rpc_is_access_failure_before_work(self):
        self.settings(error_method='session/new', rpc_error=dict(code=-32000, message='auth_required'))
        result = self.result(self.start())
        self.assertEqual(result['failure_kind'], 'access', result)
        self.assertFalse(result['work_started'])

    def test_prompt_rpc_failure_is_provider_failure_with_work_started(self):
        self.settings(error_method='session/prompt', rpc_error=dict(code=-32603, message='Unexpected ACP failure'))
        result = self.result(self.start())
        self.assertEqual(result['failure_kind'], 'provider', result)
        self.assertTrue(result['work_started'])

    def test_null_initialize_result_preserves_durable_protocol_failure(self):
        self.settings(result_method='initialize', result_value=None)
        result = self.result(self.start())
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['failure_kind'], 'invalid')
        self.assertFalse(result['work_started'])

    def test_null_session_update_preserves_durable_protocol_failure(self):
        self.settings(update_value=None)
        result = self.result(self.start())
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['failure_kind'], 'invalid')
        self.assertTrue(result['work_started'])

    def test_null_notification_params_preserves_durable_protocol_failure(self):
        self.settings(notification_params=None)
        result = self.result(self.start())
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['failure_kind'], 'invalid')

    def test_null_message_content_preserves_durable_protocol_failure(self):
        self.settings(update_value=dict(sessionUpdate='agent_message_chunk', content=None))
        result = self.result(self.start())
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['failure_kind'], 'invalid')

    def test_null_permission_params_preserves_durable_protocol_failure(self):
        self.settings(permission=True, permission_params=None)
        result = self.result(self.start())
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['failure_kind'], 'invalid')

    def test_acp_request_cannot_enter_legacy_wire_permission_handler(self):
        self.settings(reverse_request=dict(jsonrpc='2.0', id='unsupported-1', method='request', params=None))
        result = self.result(self.start())
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['failure_kind'], 'permission')
        self.assertFalse((self.directory / 'approval.json').exists())

    def test_protocol_error_code_takes_precedence_over_incidental_401(self):
        self.settings(error_method='session/new',
                      rpc_error=dict(code=-32602, message='Invalid parameters: unknown option 401'))
        result = self.result(self.start())
        self.assertEqual(result['failure_kind'], 'invalid', result)
        self.assertFalse(result['work_started'])

    def test_internal_rpc_error_with_incidental_401_is_not_access_failure(self):
        self.settings(error_method='session/new',
                      rpc_error=dict(code=-32603, message='Internal error while reading item 401'))
        result = self.result(self.start())
        self.assertEqual(result['failure_kind'], 'provider', result)

    def test_resource_error_with_401_in_path_is_not_access_failure(self):
        self.settings(error_method='session/new',
                      rpc_error=dict(code=-32002, message='Resource not found', data=dict(uri='file:///401')))
        result = self.result(self.start())
        self.assertEqual(result['failure_kind'], 'provider', result)

    def test_rpc_error_requires_integer_code_and_text_message(self):
        self.settings(error_method='session/new', rpc_error=dict(code='401', message=None))
        result = self.result(self.start())
        self.assertEqual(result['failure_kind'], 'invalid', result)

    def test_protocol_mismatch_does_not_start_work(self):
        self.settings(protocol=99)
        result = self.result(self.start())
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn('protocol', result['error'].lower())
        self.assertFalse(result['work_started'])
        self.assertFalse(any(call.get('method') == 'session/new' for call in self.calls()))

    def test_cancelled_prompt_cannot_establish_completion(self):
        self.settings(stop_reason='cancelled')
        result = self.result(self.start())
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['failure_kind'], 'cancelled')

    def test_nonzero_exit_after_result_cannot_establish_completion(self):
        self.settings(exit_code=9)
        result = self.result(self.start())
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['failure_kind'], 'provider')
        self.assertEqual(result['native_returncode'], 9)

    def test_owner_pipe_loss_cancels_in_flight_work_without_replay(self):
        self.settings(wait=True)
        process = self.start()
        self.wait_for(self.base / 'ready', process)
        self.close_owner()
        result = self.result(process)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['failure_kind'], 'uncertain_work')
        cancel = [call for call in self.calls() if call.get('method') == 'session/cancel']
        self.assertEqual(len(cancel), 1)
        self.assertNotIn('id', cancel[0])
        self.assertEqual(cancel[0]['params'], dict(sessionId='acp-session'))
        self.assertEqual(sum(call.get('method') == 'session/prompt' for call in self.calls()), 1)

    def test_ledger_stop_cancels_pending_permission_with_protocol_reply(self):
        self.settings(permission=True)
        process = self.start()
        self.wait_for(self.directory / 'approval.json', process)
        self.state['enabled'] = False
        control.atomic_json(self.root / 'state.json', self.state)
        result = self.result(process)
        self.assertEqual(result['failure_kind'], 'cancelled', result)
        self.assertEqual(self.permission_reply()['result'], dict(outcome=dict(outcome='cancelled')))
        self.assertTrue(any(call.get('method') == 'session/cancel' for call in self.calls()))


if __name__ == '__main__':
    unittest.main()

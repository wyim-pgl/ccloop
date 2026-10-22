"""Managed work exercises native process boundaries and the durable ledger."""
import json
import os
import pty
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
RUN = '''import json,pathlib,sys,types
sys.path.insert(0,sys.argv[1])
import managed
managed.providers.kimi_preflight=lambda:None  # Fake native executable has no Kimi hooks/config.
args=types.SimpleNamespace(driver=sys.argv[3],project=pathlib.Path(sys.argv[4]),goal="Finish the fixture",context_threshold=.7,access_timeout=5)
raise SystemExit(managed.run(args,json.loads(sys.argv[5]),sys.argv[2],{}))
'''

# Fake the documented CLI/RPC boundary, retaining real processes and all ledger code.
NATIVE = r'''#!/usr/bin/env python3
import json,os,pathlib,sys,time
base=pathlib.Path(os.environ['MANAGED_FIXTURE'])
provider=pathlib.Path(sys.argv[0]).name
settings=json.loads((base/'behavior.json').read_text())
def log(event):
    with (base/'calls.jsonl').open('a') as f:f.write(json.dumps(dict(provider=provider,**event))+'\n')
def emit(value):print(json.dumps(value),flush=True)
def answer(prompt):
    if os.environ.get('CCLOOP_REVIEWER') and not prompt.lstrip().startswith('{'):
        log(dict(kind='readiness',argv=sys.argv[1:]))
        if settings.get('readiness_access')==provider:
            print('authentication required',file=sys.stderr);sys.exit(1)
        return dict(status='ready')
    packet=json.loads(prompt)
    if os.environ.get('CCLOOP_REVIEWER'):
        log(dict(kind='review',packet=packet,argv=sys.argv[1:]))
        if packet['stage']=='question':
            return dict(round=packet['round'],artifact_sha256=packet['artifact_sha256'],previous_review_sha256=packet['previous_review_sha256'],question=packet['question'],answer=settings.get('answer','Use blue, as the fixture requires.'),rationale='The supplied fixture says blue.',assumptions=[],requires_user_input=False,user_input_reason='')
        final=packet['stage']=='final'
        if settings.get('reject_first_final') and len([c for c in calls() if c.get('kind')=='review' and c['packet']['stage']=='final'])==1:final=False
        prior=packet.get('last_review') or {}
        return dict(round=packet['round'],artifact_sha256=packet['artifact_sha256'],previous_review_sha256=packet['previous_review_sha256'],summary='Inspected the supplied fixture',reconciliation='Retained prior registers',progress=True,checks=[dict(method='Read supplied artifact text',result='Fixture inspected',evidence='packet.observed_files')],criteria={c['id']:dict(status='met' if final else 'not_met',evidence='Supplied artifact text') for c in packet['success_criteria']},claims=prior.get('claims',[]),issues=prior.get('issues',[]),next_milestone='Finish the fixture')
    n=len([x for x in calls() if x.get('kind')=='work'])
    log(dict(kind='work',packet=packet,argv=sys.argv[1:]))
    if settings.get('wait'):
        (base/'ready').write_text(str(os.getpid()))
        while not (base/'release').exists():time.sleep(.02)
    (base/'project'/settings.get('target','artifact.txt')).write_text('work '+str(n+1))
    seq=settings.get('statuses',['continue','complete'])
    status=seq[min(n,len(seq)-1)]
    value=dict(binding=packet['binding'],summary='Turn '+str(n+1),next_steps=['Continue the fixture'],completed=['Wrote artifact '+str(n+1)],working_notes='Remember blue '+str(n+1),status=status,question='Which color?' if status=='question' else '',checks=[dict(method='Read artifact.txt',result='work '+str(n+1),evidence='artifact.txt')])
    if settings.get('wrong_binding'):value['binding']['generation']+=1
    return value
def calls():
    return [json.loads(x) for x in (base/'calls.jsonl').read_text().splitlines()] if (base/'calls.jsonl').exists() else []
if os.environ.get('CCLOOP_REVIEWER'):
    value=answer(sys.stdin.read())
    if provider=='claude':emit(dict(type='result',is_error=False,result=json.dumps(value),modelUsage={sys.argv[sys.argv.index('--model')+1]:{}}))
    elif provider=='codex':emit(dict(type='item.completed',item=dict(type='agent_message',text=json.dumps(value))))
    else:emit(dict(role='assistant',content=[dict(type='text',text=json.dumps(value))]))
    sys.exit(0)
if settings.get('access')==provider:
    print('authentication required',file=sys.stderr);sys.exit(1)
if provider=='claude':
    value=answer(sys.stdin.read())
    if settings.get('native_trace') and sys.argv[sys.argv.index('--output-format')+1]=='stream-json':
        emit(dict(type='assistant',message=dict(role='assistant',content=[dict(type='tool_use',id='read-1',name='Read',input=dict(file_path='artifact.txt'))])))
        emit(dict(type='user',message=dict(role='user',content=[dict(type='tool_result',tool_use_id='read-1',content='ACTUAL_NATIVE_TRACE '+(base/'project'/'artifact.txt').read_text())])))
    emit(dict(type='result',is_error=False,structured_output=value,modelUsage={sys.argv[sys.argv.index('--model')+1]:dict(inputTokens=999999999)},session_id='claude-session'))
    sys.exit(settings.get('exit_code',0))
for line in sys.stdin:
    msg=json.loads(line);method=msg.get('method');p=msg.get('params',{})
    if method=='initialize':emit(dict(jsonrpc='2.0',id=msg['id'],result={}))
    elif method in ('thread/start','thread/resume'):
        thread=p.get('threadId','thread-'+str(len(calls())))
        log(dict(kind=method,params=p))
        emit(dict(id=msg['id'],result=dict(thread=dict(id=thread),model=p['model'])))
        if settings.get('slow_read'):
            (base/'ready').write_text(str(os.getpid()))
            time.sleep(30)
    elif method in ('turn/start','prompt'):
        if settings.get('permission_request'):
            emit(dict(id=99,method='item/commandExecution/requestApproval',params=dict(threadId=thread,turnId='turn-1',itemId='cmd',command='touch approved.txt',cwd=os.getcwd())))
            reply=json.loads(sys.stdin.readline())
            log(dict(kind='permission_answer',reply=reply))
        prompt=p['input'][0]['text'] if provider=='codex' else p['user_input']
        value=answer(prompt)
        if provider=='codex':
            emit(dict(id=msg['id'],result=dict(turn=dict(id='turn-1',status='inProgress',items=[],error=None))))
            usage=settings.get('usage',[20,80,20]);n=len([x for x in calls() if x.get('kind')=='work'])-1
            b=dict(totalTokens=usage[min(n,len(usage)-1)],inputTokens=10,cachedInputTokens=0,outputTokens=10,reasoningOutputTokens=0)
            emit(dict(method='thread/tokenUsage/updated',params=dict(threadId=thread,turnId='turn-1',tokenUsage=dict(last=b,total=dict(b,totalTokens=999999999),modelContextWindow=100))))
            if settings.get('private_reasoning'):
                emit(dict(method='item/reasoning/textDelta',params=dict(threadId=thread,turnId='turn-1',itemId='reason',delta='PRIVATE_REASONING_FIXTURE')))
                emit(dict(method='item/completed',params=dict(threadId=thread,turnId='turn-1',item=dict(type='reasoning',text='PRIVATE_REASONING_FIXTURE'))))
            emit(dict(method='item/completed',params=dict(threadId=thread,turnId='turn-1',item=dict(type='agentMessage',id='item-1',text=json.dumps(value),phase='final_answer'))))
            emit(dict(method='turn/completed',params=dict(threadId=thread,turn=dict(id='turn-1',status='completed',items=[],error=None))))
            if settings.get('delayed_exit'):
                time.sleep(.8);sys.exit(9)
        else:
            log(dict(kind='wire',params=p,argv=sys.argv[1:]))
            emit(dict(jsonrpc='2.0',method='event',params=dict(type='ContentPart',payload=dict(type='text',text=json.dumps(value)))))
            emit(dict(jsonrpc='2.0',id=msg['id'],result=dict(status='finished')))
'''


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        (self.project / 'artifact.txt').write_text('initial')
        self.root = self.base / 'ledger'
        self.models = {'claude': 'claude-fixture', 'codex': 'codex-fixture', 'kimi': 'kimi-fixture'}
        self.env = dict(os.environ, MANAGED_FIXTURE=str(self.base))
        self.env.pop('CLAUDECODE', None)
        for provider in self.models:
            file = self.base / provider
            file.write_text(NATIVE)
            file.chmod(0o700)
            self.env['CCLOOP_' + provider.upper() + '_BIN'] = str(file)
        self.settings()
        self.init('codex')

    def settings(self, **kwargs):
        (self.base / 'behavior.json').write_text(json.dumps(kwargs))

    def init(self, driver):
        config = self.base / 'config.json'
        config.write_text(json.dumps(dict(project=str(self.project),driver=driver,goal='Finish the fixture',authorization='Edit the fixture',success_criteria=[dict(id='fixture',description='Fixture complete')],watch=['*.txt'],auto_update_models=False,models=self.models,commands={x:[str(self.base/x)] for x in self.models})))
        result = subprocess.run([sys.executable,str(SCRIPTS/'ccloop.py'),'--root',str(self.root),'init','--config',str(config)],env=self.env,capture_output=True,text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def start(self, driver='codex'):
        process = subprocess.Popen([sys.executable,'-c',RUN,str(SCRIPTS),str(self.root),driver,str(self.project),json.dumps(self.models)],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(lambda: self.cleanup_process(process))
        return process

    def cleanup_process(self, process):
        if process.poll() is None:
            process.kill()
        process.communicate()

    def run_manager(self, driver='codex'):
        p=self.start(driver)
        stdout,stderr=p.communicate(timeout=45)
        return p.returncode,stdout,stderr

    def state(self):
        return json.loads((self.root/'state.json').read_text())

    def calls(self):
        file=self.base/'calls.jsonl'
        return [json.loads(x) for x in file.read_text().splitlines()] if file.exists() else []

    def wait_for(self, predicate):
        end=time.monotonic()+10
        while time.monotonic()<end:
            if predicate():return
            time.sleep(.02)
        self.fail('Timed out waiting for native fixture')

    def test_codex_rotates_using_current_context_and_restores_all_files(self):
        self.settings(statuses=['continue','continue','complete'])
        before=self.state()
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        calls=self.calls()
        self.assertEqual([c['kind'] for c in calls if c['kind'].startswith('thread/')],['thread/start','thread/resume','thread/start'])
        work=[c for c in calls if c['kind']=='work']
        self.assertEqual(len(work),3)
        for call in work:
            self.assertEqual(set(call['packet']['context']['files']),{'resume.md','intend.md','todo.md','quarantine.md'})
            self.assertIn('Finish the fixture',call['packet']['context']['files']['intend.md'])
        self.assertIn('Remember blue 2',work[2]['packet']['context']['files']['resume.md'])
        state=self.state()
        self.assertEqual(state['phase'],'complete')
        self.assertEqual(state['run_id'],before['run_id'])
        self.assertEqual(state['config'],before['config'])
        self.assertEqual(state['created_at'],before['created_at'])
        self.assertEqual([c['packet']['stage'] for c in calls if c['kind']=='review'],['plan','final'])
        self.assertEqual(state['managed']['context_policy'],'measured-current-context')

    def test_claude_fresh_turns_do_not_treat_billing_as_context(self):
        import shutil
        shutil.rmtree(self.root)
        self.init('claude')
        code,_,error=self.run_manager('claude')
        self.assertEqual(code,0,error)
        work=[c for c in self.calls() if c['kind']=='work']
        self.assertEqual(len(work),2)
        self.assertTrue(all('--resume' not in c['argv'] and '--continue' not in c['argv'] for c in work))
        self.assertEqual(self.state()['managed']['context_policy'],'fresh-per-turn-no-measurement')
        self.assertTrue(all('--dangerously-skip-permissions' not in c['argv'] for c in work))

    def test_question_answer_enters_next_turn_and_never_completes_run(self):
        self.settings(statuses=['question','complete'])
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        calls=self.calls()
        question=[c for c in calls if c['kind']=='review' and c['packet']['stage']=='question']
        self.assertEqual([c['provider'] for c in question],['claude'])
        work=[c for c in calls if c['kind']=='work']
        self.assertIn('Use blue, as the fixture requires.',json.dumps(work[1]['packet']))
        self.assertEqual(self.state()['question_count'],1)
        self.assertEqual([c['packet']['stage'] for c in calls if c['kind']=='review'][-1],'final')

    def test_worker_completion_waits_for_independent_final_acceptance(self):
        self.settings(statuses=['complete'],reject_first_final=True)
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        self.assertEqual(len([c for c in self.calls() if c['kind']=='work']),2)
        self.assertEqual(len([c for c in self.calls() if c['kind']=='review' and c['packet']['stage']=='final']),2)

    def test_completed_result_after_manager_crash_is_consumed_without_repeating_work(self):
        self.settings(wait=True,statuses=['complete'])
        p=self.start()
        self.wait_for(lambda:(self.base/'ready').exists() or p.poll() is not None)
        self.assertIsNone(p.poll(),p.communicate()[1] if p.poll() is not None else '')
        p.send_signal(signal.SIGSTOP)
        (self.base/'release').touch()
        self.wait_for(lambda:list((self.root/'managed').glob('*/result.json')))
        p.kill();p.communicate(timeout=10)
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        self.assertEqual(len([c for c in self.calls() if c['kind']=='work']),1)

    def test_second_manager_cannot_stop_or_duplicate_owned_work(self):
        self.settings(wait=True,statuses=['complete'])
        p=self.start()
        self.wait_for(lambda:(self.base/'ready').exists() or p.poll() is not None)
        self.assertIsNone(p.poll(),p.communicate()[1] if p.poll() is not None else '')
        before=self.state()
        code,_,error=self.run_manager()
        self.assertNotEqual(code,0,error)
        self.assertEqual(self.state(),before)
        (self.base/'release').touch()
        _,error=p.communicate(timeout=20)
        self.assertEqual(p.returncode,0,error)
        self.assertEqual(len([c for c in self.calls() if c['kind']=='work']),1)

    def test_native_permission_request_is_never_automatically_approved(self):
        self.settings(permission_request=True)
        code,_,error=self.run_manager()
        self.assertNotEqual(code,0,error)
        self.assertEqual(self.state()['failure_kind'],'permission')
        self.assertEqual((self.project/'artifact.txt').read_text(),'initial')
        self.assertFalse(any(c['kind']=='permission_answer' and c['reply'].get('result',{}).get('decision')=='accept' for c in self.calls()))

    def test_unanswered_terminal_permission_does_not_prevent_user_stop(self):
        self.settings(permission_request=True)
        terminal,slave=pty.openpty()
        terminal_settings=termios.tcgetattr(slave)
        terminal_settings[3] &= ~termios.ICANON
        termios.tcsetattr(slave,termios.TCSANOW,terminal_settings)
        self.addCleanup(os.close,terminal)
        self.addCleanup(os.close,slave)
        p=subprocess.Popen([sys.executable,'-c',RUN,str(SCRIPTS),str(self.root),'codex',str(self.project),json.dumps(self.models)],env=self.env,stdin=slave,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(lambda:self.cleanup_process(p))
        self.wait_for(lambda:list((self.root/'managed').glob('*/approval.json')) or p.poll() is not None)
        self.assertIsNone(p.poll(),p.communicate()[1] if p.poll() is not None else '')
        os.write(terminal,b'y')  # A partial line must not block the stop poll.
        time.sleep(.2)
        p2=subprocess.run([sys.executable,str(SCRIPTS/'ccloop.py'),'--root',str(self.root),'stop','--reason','User stop while permission waits'],env=self.env,capture_output=True,text=True)
        self.assertEqual(p2.returncode,0,p2.stderr)
        self.wait_for(lambda:p.poll() is not None)
        self.assertEqual(self.state()['phase'],'stopped')

    def test_nonzero_native_exit_cannot_establish_completion(self):
        import shutil
        shutil.rmtree(self.root)
        self.init('claude')
        self.settings(statuses=['complete'],exit_code=1)
        code,_,error=self.run_manager('claude')
        self.assertNotEqual(code,0,error)
        self.assertNotEqual(self.state()['phase'],'complete')
        self.assertFalse(any(c['kind']=='review' and c['packet']['stage']=='final' for c in self.calls()))

    def test_claude_native_tool_trace_reaches_independent_review(self):
        import shutil
        shutil.rmtree(self.root)
        self.init('claude')
        self.settings(statuses=['complete'],native_trace=True)
        code,_,error=self.run_manager('claude')
        self.assertEqual(code,0,error)
        final=next(c['packet']['packet'] for c in self.calls() if c['kind']=='review' and c['packet']['stage']=='final')
        self.assertIn('ACTUAL_NATIVE_TRACE work 1',final['native_events_excerpt'])

    def test_wrong_frozen_goal_leaves_the_existing_run_unchanged(self):
        before=self.state()
        source=RUN.replace('goal="Finish the fixture"','goal="Another goal"')
        p=subprocess.run([sys.executable,'-c',source,str(SCRIPTS),str(self.root),'codex',str(self.project),json.dumps(self.models)],env=self.env,capture_output=True,text=True,timeout=20)
        self.assertNotEqual(p.returncode,0,p.stderr)
        self.assertEqual(self.state(),before)

    def test_owner_liveness_does_not_require_linux_process_identity(self):
        self.settings(statuses=['complete'])
        source=RUN.replace('import managed','import managed\nmanaged.providers.process_identity=lambda pid:None')
        p=subprocess.run([sys.executable,'-c',source,str(SCRIPTS),str(self.root),'codex',str(self.project),json.dumps(self.models)],env=self.env,capture_output=True,text=True,timeout=45)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(self.state()['phase'],'complete')

    def test_initial_auth_failure_uses_kimi_before_any_native_work(self):
        self.settings(statuses=['complete'],readiness_access='codex')
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        self.assertEqual([c['provider'] for c in self.calls() if c['kind']=='work'],['kimi'])
        self.assertEqual(self.state()['active_driver'],'kimi')

    def test_configured_codex_model_is_not_claimed_as_observed_service_model(self):
        self.settings(statuses=['complete'])
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        native=self.state()['managed']['last_native']
        self.assertEqual(native['observed_models'],[])
        self.assertEqual(native['selected_model'],'codex-fixture')

    def test_journal_keeps_intermediate_worker_answer_check_and_model(self):
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        journal=(self.root/'ccloop.md').read_text()
        self.assertIn('Remember blue 1',journal)
        self.assertIn('"result": "work 1"',journal)
        self.assertIn('"selected_model": "codex-fixture"',journal)

    def test_delayed_nonzero_native_exit_cannot_establish_completion(self):
        self.settings(statuses=['complete'],delayed_exit=True)
        code,_,error=self.run_manager()
        self.assertNotEqual(code,0,error)
        self.assertNotEqual(self.state()['phase'],'complete')
        self.assertFalse(any(c['kind']=='review' and c['packet']['stage']=='final' for c in self.calls()))

    def test_private_reasoning_events_are_excluded_from_runtime_audit(self):
        self.settings(statuses=['complete'],private_reasoning=True)
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn('PRIVATE_REASONING_FIXTURE',path.read_text(errors='replace'),str(path))

    def test_public_audit_filters_thinking_but_preserves_tool_json_schema(self):
        source='import json,sys;sys.path.insert(0,sys.argv[1]);import native_driver;print(json.dumps(native_driver.public_audit(json.load(sys.stdin))))'
        packet={'type':['string','null'],'content':[{'type':'thinking','thinking':'private'},{'type':'redacted_thinking','data':'private'},{'type':'think','think':'private'},{'type':'tool_result','content':'visible result'}]}
        p=subprocess.run([sys.executable,'-c',source,str(SCRIPTS)],input=json.dumps(packet),env=self.env,capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads(p.stdout),{'type':['string','null'],'content':[{'type':'tool_result','content':'visible result'}]})

    def test_changed_artifact_is_in_review_evidence_even_after_large_sorted_prefix(self):
        for n in range(31):(self.project/('a%02d.txt'%n)).write_text('background')
        self.settings(statuses=['complete'],target='z-result.txt')
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        final=next(c['packet']['packet'] for c in self.calls() if c['kind']=='review' and c['packet']['stage']=='final')
        self.assertTrue(any(x['path']=='z-result.txt' and x['text']=='work 1' for x in final['observed_files']))
        self.assertGreater(final['coverage']['omitted_count'],0)

    def test_large_peer_answer_does_not_bypass_recovery_prompt_bound(self):
        self.settings(statuses=['question','complete'],answer='a'*200000)
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        work=[c for c in self.calls() if c['kind']=='work']
        self.assertLess(len(json.dumps(work[1]['packet']).encode()),96*1024)

    def test_reconcile_retires_only_inspected_turn_then_explicit_resume_can_continue(self):
        self.settings(wrong_binding=True,statuses=['complete'])
        code,_,error=self.run_manager()
        self.assertNotEqual(code,0,error)
        before=self.state();turn=before['managed']['pending']['id']
        packet=self.base/'reconcile.json'
        packet.write_text(json.dumps(dict(summary='Inspected partial fixture',completed=['Inspected artifact.txt'],next_steps=['Finish the fixture'],working_notes='Existing edit retained',checks=[dict(method='Read artifact.txt',result='work 1',evidence='Inspected file contains work 1')])) )
        source='import pathlib,sys;sys.path.insert(0,sys.argv[1]);import managed;managed.reconcile(pathlib.Path(sys.argv[2]),pathlib.Path(sys.argv[3]),"Inspected partial work",sys.argv[4])'
        p=subprocess.run([sys.executable,'-c',source,str(SCRIPTS),str(self.root),str(packet),turn],env=self.env,capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,0,p.stderr)
        after=self.state()
        self.assertEqual(after['phase'],before['phase'])
        self.assertEqual(after['enabled'],before['enabled'])
        self.assertEqual(after['round'],before['round'])
        self.assertEqual(after['next_review_at'],before['next_review_at'])
        self.assertIsNone(after['managed']['pending'])
        self.settings(statuses=['complete'])
        p=subprocess.run([sys.executable,str(SCRIPTS/'ccloop.py'),'--root',str(self.root),'resume','--reason','Inspected prior outcome'],env=self.env,capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        self.assertEqual(len([c for c in self.calls() if c['kind']=='work']),2)

    def test_handoff_during_checkpoint_does_not_accept_superseded_work(self):
        self.settings(statuses=['complete'])
        insertion='''original=managed.ccloop.checkpoint
def handoff(*a,**kw):
    result=original(*a,**kw)
    managed.ccloop.change_state(a[0],'handoff','Explicit external handoff',driver='claude')
    return result
managed.ccloop.checkpoint=handoff
'''
        source=RUN.replace('raise SystemExit',insertion+'raise SystemExit')
        p=subprocess.run([sys.executable,'-c',source,str(SCRIPTS),str(self.root),'codex',str(self.project),json.dumps(self.models)],env=self.env,capture_output=True,text=True,timeout=45)
        self.assertNotEqual(p.returncode,0,p.stderr)
        self.assertEqual(self.state()['active_driver'],'claude')
        self.assertFalse(any(event['action']=='managed_turn_accepted' for event in self.state()['history']))

    def test_partial_acceptance_recovery_rejects_changed_artifacts(self):
        self.settings(statuses=['complete'])
        insertion='''original=managed.ccloop.checkpoint
def crash(*a,**kw):
    original(*a,**kw)
    __import__('os')._exit(19)
managed.ccloop.checkpoint=crash
'''
        source=RUN.replace('raise SystemExit',insertion+'raise SystemExit')
        p=subprocess.run([sys.executable,'-c',source,str(SCRIPTS),str(self.root),'codex',str(self.project),json.dumps(self.models)],env=self.env,capture_output=True,text=True,timeout=45)
        self.assertEqual(p.returncode,19,p.stderr)
        (self.project/'artifact.txt').write_text('externally changed')
        code,_,error=self.run_manager()
        self.assertNotEqual(code,0,error)
        self.assertFalse(any(event['action']=='managed_turn_accepted' for event in self.state()['history']))

    def test_wrong_generation_result_cannot_be_accepted_or_replayed(self):
        self.settings(wrong_binding=True,statuses=['complete'])
        code,_,error=self.run_manager()
        self.assertNotEqual(code,0,error)
        self.assertNotEqual(self.state()['phase'],'complete')
        self.run_manager()
        self.assertEqual(len([c for c in self.calls() if c['kind']=='work']),1)

    def test_missing_driver_falls_back_to_kimi_wire_without_self_review(self):
        (self.base/'codex').unlink()
        self.settings(statuses=['complete'])
        code,_,error=self.run_manager()
        self.assertEqual(code,0,error)
        self.assertEqual(self.state()['active_driver'],'kimi')
        work=[c for c in self.calls() if c['kind']=='work']
        self.assertEqual([c['provider'] for c in work],['kimi'])
        self.assertTrue(all('--wire' in c['argv'] and '--print' not in c['argv'] and '--yolo' not in c['argv'] for c in work))
        self.assertTrue(all(c['provider']!='kimi' for c in self.calls() if c['kind']=='review'))

    def test_stop_cancels_running_native_child_and_prevents_next_turn(self):
        self.settings(wait=True)
        p=self.start()
        self.wait_for(lambda:(self.base/'ready').exists() or p.poll() is not None)
        self.assertIsNone(p.poll(),p.communicate()[1] if p.poll() is not None else '')
        stopped=subprocess.run([sys.executable,str(SCRIPTS/'ccloop.py'),'--root',str(self.root),'stop','--reason','User stop'],env=self.env,capture_output=True,text=True)
        self.assertEqual(stopped.returncode,0,stopped.stderr)
        p.communicate(timeout=10)
        self.assertEqual(self.state()['phase'],'stopped')
        self.assertEqual(len([c for c in self.calls() if c['kind']=='work']),1)
        self.assertEqual((self.project/'artifact.txt').read_text(),'initial')

    def test_clear_request_forces_fresh_session_on_next_accepted_turn(self):
        self.settings(wait=True, usage=[20,20,20], statuses=['continue','complete'])
        process=self.start()
        self.wait_for(lambda: (self.base/'ready').exists())
        packet=self.base/'clear.json'
        packet.write_text(json.dumps(dict(summary='User requested a clear',next_steps=['Finish the fixture'],completed=[],working_notes='',quarantine=['STALE_NOTE_907'])))
        result=subprocess.run([sys.executable,str(SCRIPTS/'ccloop.py'),'--root',str(self.root),'clear','--packet',str(packet),'--reason','User asked for /ccloop clear'],env=self.env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['clear_result']['mode'],'managed')
        self.assertTrue((self.root/'managed'/'rotate-requested.json').is_file())
        (self.base/'release').write_text('go')
        stdout,stderr=process.communicate(timeout=45)
        self.assertEqual(process.returncode,0,stderr)
        calls=self.calls()
        self.assertEqual([c['kind'] for c in calls if c['kind'].startswith('thread/')],['thread/start','thread/start'])
        state=self.state()
        self.assertEqual(state['phase'],'complete')
        self.assertFalse((self.root/'managed'/'rotate-requested.json').exists())
        accepted=[e for e in state['history'] if e['action']=='managed_turn_accepted']
        self.assertEqual([(e['rotate'],e.get('rotate_requested')) for e in accepted],[(True,'User asked for /ccloop clear'),(False,None)])
        self.assertIn('User asked for /ccloop clear',(self.root/'ccloop.md').read_text())
        self.assertNotIn('STALE_NOTE_907',(self.root/'quarantine.md').read_text())

    def test_dead_manager_uncertain_work_is_not_replayed(self):
        self.settings(wait=True)
        p=self.start()
        self.wait_for(lambda:(self.base/'ready').exists() or p.poll() is not None)
        self.assertIsNone(p.poll(),p.communicate()[1] if p.poll() is not None else '')
        p.kill();p.communicate(timeout=10)
        self.wait_for(lambda:not self.native_alive(int((self.base/'ready').read_text())))
        code,_,error=self.run_manager()
        self.assertNotEqual(code,0,error)
        self.assertEqual(len([c for c in self.calls() if c['kind']=='work']),1)
        self.assertEqual(self.state()['failure_kind'],'uncertain_work')

    def test_blocked_native_input_still_cancels_when_manager_dies(self):
        self.settings(slow_read=True)
        source=RUN.replace('raise SystemExit',"original=managed.work_prompt\nmanaged.work_prompt=lambda *a:original(*a)+' '*300000\nraise SystemExit")
        p=subprocess.Popen([sys.executable,'-c',source,str(SCRIPTS),str(self.root),'codex',str(self.project),json.dumps(self.models)],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(lambda:self.cleanup_process(p))
        self.wait_for(lambda:(self.base/'ready').exists() or p.poll() is not None)
        self.assertIsNone(p.poll(),p.communicate()[1] if p.poll() is not None else '')
        pid=int((self.base/'ready').read_text())
        self.addCleanup(lambda:self.kill_native(pid))
        time.sleep(.3)
        p.kill();p.communicate(timeout=10)
        self.wait_for(lambda:not self.native_alive(pid))

    @staticmethod
    def kill_native(pid):
        try:os.killpg(pid,signal.SIGKILL)
        except ProcessLookupError:pass

    @staticmethod
    def native_alive(pid):
        try:
            return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()[0]!='Z'
        except FileNotFoundError:return False


if __name__=='__main__':unittest.main()

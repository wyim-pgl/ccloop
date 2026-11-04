"""Native work adapters, separate from the isolated, tool-free reviewers.

Codex protocol fields follow the CLI's generated app-server JSON schemas.
Kimi Code uses ACP; the legacy Python client retains its Wire transport.
The supervisor owns native children and persists a result before exiting.
"""
import json
import os
from pathlib import Path
import selectors
import select
import signal
import stat
import subprocess
import sys
import time

import control
import checkpoints
import providers
import runtime_paths
import kimi_runtime


MAX_RESULT = 2_000_000


def private_reasoning(value):
    return (isinstance(value, dict) and ((isinstance(value.get('type'), str)
            and value['type'] in {'reasoning', 'thinking', 'redacted_thinking', 'think'})
            or value.get('sessionUpdate') == 'agent_thought_chunk'
            or str(value.get('method', '')).startswith('item/reasoning/')))


def public_audit(value):
    """Keep visible answers and tool facts; never persist model thinking blocks."""
    if private_reasoning(value):
        return None
    if isinstance(value, dict):
        return {key: public_audit(child) for key, child in value.items() if not private_reasoning(child)}
    if isinstance(value, list):
        return [public_audit(child) for child in value if not private_reasoning(child)]
    return value


class DriverError(ValueError):
    def __init__(self, message, kind='invalid'):
        super().__init__(message)
        self.kind = kind


def acp_object(value, field):
    if not isinstance(value, dict):
        raise DriverError('Invalid Kimi ACP ' + field + ': expected an object')
    return value


def acp_error_kind(value):
    """Use ACP error codes, never incidental numbers in messages or data."""
    error = acp_object(value, 'error')
    code = error.get('code')
    if type(code) is not int or not isinstance(error.get('message'), str):
        raise DriverError('Invalid Kimi ACP error: expected an integer code and text message')
    if code == -32000:
        return 'access'  # ACP authRequired
    if code == -32800:
        return 'cancelled'
    if code == -32700 or -32699 <= code <= -32600:
        return 'provider' if code == -32603 else 'invalid'
    return 'provider'


def private_json(path, maximum=MAX_RESULT):
    """Do not follow a replaced artifact symlink or block on a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise DriverError('Invalid or oversized private result artifact')
        return json.load(stream)


def alive(pid, identity):
    return bool(identity and providers.process_identity(pid) == identity)


class Native:
    def __init__(self, job, directory, owner_fd):
        self.job, self.directory = job, directory
        self.owner_fd = owner_fd
        os.set_blocking(owner_fd, False)
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.buffer = b''
        self.serial = 0
        self.end = time.monotonic() + min(job['timeout_seconds'], job['access_timeout'])
        self.details = {'requested_model': job['model'], 'observed_models': [],
                        'context_usage': None, 'session_id': None, 'work_started': False}
        self.events = []
        self.responses = {}
        self.error = None
        self.err = None
        self.log = None
        self.acp = False
        self.acp_config = []
        self.acp_model_selected = False
        self.acp_approval_ids = set()

    def check(self):
        try:
            owner_gone = os.read(self.owner_fd, 1) == b''
        except BlockingIOError:
            owner_gone = False
        if owner_gone:
            raise DriverError('Manager disappeared during work; do not replay this turn', 'uncertain_work')
        state = private_json(Path(self.job['root']) / 'state.json', 64_000_000)
        binding = self.job['binding']
        if not state['enabled'] or state['generation'] != binding['generation'] or state['run_id'] != binding['run_id']:
            raise DriverError('Run stopped or superseded during work', 'cancelled')
        if state['revision'] != self.job['expected_revision']:
            raise DriverError('Ledger changed while native work was in flight', 'uncertain_work')
        if state.get('deadline') is not None and time.time() >= state['deadline']:
            raise DriverError('Original wall-clock limit reached', 'cancelled')
        if time.monotonic() >= self.end:
            raise DriverError('Bounded native turn timed out; reconcile its effects before continuing', 'uncertain_work')

    def start(self, argv, rpc=True):
        self.check()
        state = private_json(Path(self.job['root']) / 'state.json', 64_000_000)
        if checkpoints.safe_snapshot(state)['sha256'] != self.job['binding']['artifact_sha256']:
            raise DriverError('Artifacts changed before native worker dispatch', 'uncertain_work')
        environment = dict(os.environ, CCLOOP_MANAGED_WORKER='1')
        environment.pop('CLAUDECODE', None)
        if self.acp:
            # Resolve relative roots before Popen changes cwd to the project.
            environment['KIMI_CODE_HOME'] = str(kimi_runtime.code_home())
        environment = runtime_paths.client_env(argv[0], environment)
        self.err = open(self.directory / 'stderr.txt', 'wb')
        self.log = open(self.directory / 'stdout.jsonl', 'wb')
        try:
            self.process = subprocess.Popen(argv, cwd=self.job['project'], env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err,
                start_new_session=True)
        except (FileNotFoundError, PermissionError) as error:
            raise DriverError(str(error), 'access') from error
        control.atomic_json(self.directory / 'native.json', {
            'pid': self.process.pid, 'process_identity': providers.process_identity(self.process.pid),
            'argv': argv, 'binding': self.job['binding']})
        os.set_blocking(self.process.stdout.fileno(), False)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def send(self, value):
        self.send_bytes((json.dumps(value, ensure_ascii=False) + '\n').encode())

    def send_bytes(self, data):
        remaining = memoryview(data)
        fd = self.process.stdin.fileno()
        while remaining:
            self.check()
            try:
                written = os.write(fd, remaining)
                remaining = remaining[written:]
            except BlockingIOError:
                select.select([], [fd], [], .1)

    def request(self, method, params):
        self.serial += 1
        self.send({'jsonrpc': '2.0', 'id': self.serial, 'method': method, 'params': params})
        return self.serial

    def receive(self):
        while b'\n' not in self.buffer:
            self.check()
            if self.selector.select(.1):
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    self.process.wait(timeout=2)
                    self.err.flush()
                    error = (self.directory / 'stderr.txt').read_text(errors='replace')[-8000:]
                    kind = 'access' if providers.ACCESS.search(error) else 'provider'
                    raise DriverError(error or 'Native process exited without a completed turn', kind)
                self.buffer += data
                if len(self.buffer) > MAX_RESULT:
                    raise DriverError('Native protocol line exceeded the bounded result size')
        line, self.buffer = self.buffer.split(b'\n', 1)
        value = json.loads(line)
        if not isinstance(value, dict):
            raise DriverError('Native protocol event is not an object')
        self.audit(value)
        if 'method' in value and 'id' in value:
            self.approval(value)
        elif 'id' in value:
            self.responses[value['id']] = value
        else:
            self.event(value)
        return value

    def audit(self, value):
        safe = public_audit(value)
        if safe is not None:
            self.log.write((json.dumps(safe, ensure_ascii=False) + '\n').encode())
            self.log.flush()

    def response(self, request):
        while request not in self.responses:
            self.receive()
        value = self.responses.pop(request)
        if 'error' in value:
            error = json.dumps(public_audit(value['error']), ensure_ascii=False)
            kind = (acp_error_kind(value['error']) if self.acp else
                    'access' if providers.ACCESS.search(error) else 'provider')
            raise DriverError(error, kind)
        if self.acp:
            return acp_object(value.get('result'), 'response result')
        return value.get('result', {})

    def approval(self, value):
        """Only a person's explicit per-operation answer can approve a native request."""
        method = value['method']
        p = value.get('params', {})
        if self.acp:
            if method != 'session/request_permission':
                raise DriverError('Unsupported Kimi ACP request: ' + str(method), 'permission')
            return self.acp_approval(value)
        codex = method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}
        kimi = method == 'request' and p.get('type') == 'ApprovalRequest'
        if not (codex or kimi):
            raise DriverError('Unsupported native request; return ordinary questions in the turn report: ' + method,
                              'permission')
        request = {'id': value['id'], 'native': value, 'binding': self.job['binding']}
        control.atomic_json(self.directory / 'approval.json', request)
        answer_path = self.directory / 'approval-answer.json'
        while True:
            self.check()
            if answer_path.exists():
                answer = private_json(answer_path)
                if answer.get('id') == value['id']:
                    break
            time.sleep(.1)
        if answer.get('approve') is not True:
            raise DriverError('Native operation requires human permission; no approval was given', 'permission')
        result = ({'decision': 'accept'} if codex else
                  {'request_id': p['payload']['id'], 'response': 'approve'})
        self.send({'jsonrpc': '2.0', 'id': value['id'], 'result': result})

    def acp_approval(self, value):
        """Translate an explicit per-operation manager answer to an ACP option."""
        params = acp_object(value.get('params'), 'permission parameters')
        cancelled = {'jsonrpc': '2.0', 'id': value['id'],
                     'result': {'outcome': {'outcome': 'cancelled'}}}
        if params.get('sessionId') != self.details['session_id']:
            self.acp_shutdown_send(cancelled)
            raise DriverError('ACP permission request belongs to a different session', 'permission')
        if value['id'] in self.acp_approval_ids:
            self.acp_shutdown_send(cancelled)
            raise DriverError('ACP reused a permission request ID; a previous answer cannot approve new work', 'permission')
        self.acp_approval_ids.add(value['id'])
        control.atomic_json(self.directory / 'approval.json', {
            'id': value['id'], 'native': public_audit(value), 'binding': self.job['binding']})
        answer_path = self.directory / 'approval-answer.json'
        try:
            while True:
                self.check()
                if answer_path.exists():
                    answer = private_json(answer_path)
                    if answer.get('id') == value['id']:
                        break
                time.sleep(.1)
        except DriverError:
            # send() checks the owner and ledger, which already prohibit work.
            # A cancellation reply is cleanup only, and must still reach ACP.
            self.acp_shutdown_send(cancelled)
            raise
        approved = answer.get('approve') is True
        option_kind = 'allow_once' if approved else 'reject_once'
        options = [option for option in params.get('options', [])
                   if isinstance(option, dict) and option.get('kind') == option_kind
                   and isinstance(option.get('optionId'), str)]
        # An ordinary yes is not permission to select among several actions,
        # nor to install a persistent allow rule. Fail closed in either case.
        selected = options[0] if len(options) == 1 else None
        reply = ({'jsonrpc': '2.0', 'id': value['id'], 'result': {
            'outcome': {'outcome': 'selected', 'optionId': selected['optionId']}}}
            if selected else cancelled)
        try:
            self.send(reply)
        except DriverError:
            self.acp_shutdown_send(cancelled)
            raise
        if not approved or selected is None:
            raise DriverError('Native operation requires human permission; no approval was given', 'permission')

    def acp_shutdown_send(self, value):
        """Best-effort bounded cleanup after owner/ledger cancellation."""
        if not self.process or not self.process.stdin or self.process.stdin.closed:
            return
        data = memoryview((json.dumps(value) + '\n').encode())
        end = time.monotonic() + .2
        while data and time.monotonic() < end:
            try:
                sent = os.write(self.process.stdin.fileno(), data)
                data = data[sent:]
            except BlockingIOError:
                select.select([], [self.process.stdin.fileno()], [], .02)
            except OSError:
                break

    def acp_configuration(self, options):
        if options is None:
            return
        if not isinstance(options, list) or any(not isinstance(option, dict) for option in options):
            raise DriverError('Invalid Kimi ACP configOptions: expected a list of objects')
        self.acp_config = options
        model = next((option.get('currentValue') for option in self.acp_config
                      if option.get('id') == 'model'), None)
        if model is not None and not isinstance(model, str):
            raise DriverError('Invalid Kimi ACP model selection: expected text')
        # ACP configuration is a selected alias, not proof of a served model.
        self.details['selected_model'] = model
        if self.acp_model_selected and model and model != self.job['model']:
            raise DriverError('ACP selected a different model: ' + str(model), 'access')

    def acp_event(self, value):
        if value.get('method') != 'session/update':
            return
        params = acp_object(value.get('params'), 'session/update parameters')
        if not isinstance(params.get('sessionId'), str):
            raise DriverError('Invalid Kimi ACP update sessionId: expected text')
        if params['sessionId'] != self.details['session_id']:
            return
        update = acp_object(params.get('update'), 'session update')
        kind = update.get('sessionUpdate')
        if not isinstance(kind, str):
            raise DriverError('Invalid Kimi ACP sessionUpdate: expected text')
        if kind == 'agent_message_chunk':
            content = acp_object(update.get('content'), 'message content')
            if content.get('type') == 'text':
                if not isinstance(content.get('text'), str):
                    raise DriverError('Invalid Kimi ACP message text: expected text')
                self.details['text'] = self.details.get('text', '') + content['text']
        elif kind in {'tool_call', 'tool_call_update'}:
            self.events.append(public_audit(update))
            # ACP has no Wire StepBegin. A report must follow the last
            # tool event; earlier assistant narration remains in the audit.
            self.details['text'] = ''
        elif kind == 'config_option_update':
            if not isinstance(update.get('configOptions'), list):
                raise DriverError('Invalid Kimi ACP config option update: expected a list')
            self.acp_configuration(update['configOptions'])

    def event(self, value):
        if self.acp:
            return self.acp_event(value)
        if self.job['driver'] == 'claude':
            if value.get('type') == 'result':
                self.details['claude_result'] = value
            for block in (value.get('message') or {}).get('content', []):
                if isinstance(block, dict) and block.get('type') in {'tool_use', 'tool_result'}:
                    self.events.append({'source': 'claude/' + str(value.get('type')), **block})
        method, p = value.get('method'), value.get('params', {})
        if method == 'thread/tokenUsage/updated' and p.get('threadId') == self.details['session_id']:
            usage = p.get('tokenUsage', {})
            used, maximum = (usage.get('last') or {}).get('totalTokens'), usage.get('modelContextWindow')
            if type(used) is int and used >= 0 and type(maximum) is int and maximum > 0:
                self.details['context_usage'] = {'tokens': used, 'window': maximum,
                                                 'source': 'thread/tokenUsage/updated.last.totalTokens'}
        elif method == 'model/rerouted':
            model = p.get('toModel')
            if isinstance(model, str):
                self.details['observed_models'].append(model)
                self.error = DriverError('Native model rerouted from the selected model to ' + model, 'access')
        elif method == 'item/completed':
            item = p.get('item', {})
            if item.get('type') == 'agentMessage':
                self.details['text'] = item.get('text', '')
            elif item.get('type') in {'commandExecution', 'fileChange'}:
                self.events.append(item)
        elif method == 'turn/completed':
            self.details['turn'] = p.get('turn', {})
        elif method == 'event':
            kind, payload = p.get('type'), p.get('payload', {})
            if kind == 'StepBegin':
                self.details['text'] = ''
            elif kind == 'ContentPart' and payload.get('type') == 'text':
                self.details['text'] = self.details.get('text', '') + payload.get('text', '')
            elif kind == 'ToolResult':
                self.events.append(payload)

    def codex(self):
        self.start([self.job['executable'], 'app-server'])
        self.response(self.request('initialize', {'clientInfo': {'name': 'ccloop', 'version': '1'}}))
        self.send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})
        params = {'model': self.job['model'], 'cwd': self.job['project'],
                  'approvalPolicy': 'on-request', 'sandbox': 'workspace-write',
                  'config': {'model_reasoning_effort': 'max'}}
        previous = self.job.get('session_id')
        if previous:
            params['threadId'] = previous
        result = self.response(self.request('thread/resume' if previous else 'thread/start', params))
        self.details['session_id'] = result['thread']['id']
        self.details['selected_model'] = result.get('model')
        if result.get('model') and result['model'] != self.job['model']:
            raise DriverError('Native thread selected a different model: ' + result['model'], 'access')
        self.details['work_started'] = True
        self.end = time.monotonic() + self.job['timeout_seconds']
        self.response(self.request('turn/start', {'threadId': self.details['session_id'],
            'input': [{'type': 'text', 'text': self.job['prompt']}], 'model': self.job['model'],
            'effort': 'max', 'outputSchema': self.job['report_schema']}))
        while 'turn' not in self.details:
            self.receive()
        turn = self.details['turn']
        if turn.get('status') != 'completed':
            error = json.dumps(turn.get('error') or turn, ensure_ascii=False)
            raise DriverError(error, 'access' if providers.ACCESS.search(error) else 'provider')
        if self.error:
            raise self.error
        return json.loads(self.details.get('text', ''))

    def kimi(self):
        if kimi_runtime.kind(self.job['executable']) == 'code':
            return self.kimi_code()
        self.start([self.job['executable'], '--wire', '--model', self.job['model'], '--thinking',
                    '--work-dir', self.job['project'], '--max-steps-per-turn', '50',
                    '--max-ralph-iterations', '0'])
        self.response(self.request('initialize', {'protocol_version': '1.10',
            'client': {'name': 'ccloop', 'version': '1'}, 'capabilities': {'supports_question': False}}))
        self.details['work_started'] = True
        self.end = time.monotonic() + self.job['timeout_seconds']
        result = self.response(self.request('prompt', {'user_input': self.job['prompt']}))
        if result.get('status') != 'finished':
            raise DriverError('Kimi turn did not finish: ' + str(result.get('status')), 'uncertain_work')
        return json.loads(self.details.get('text', ''))

    def kimi_code(self):
        self.acp = True
        self.details.update(cli_kind='code', config_source=str(kimi_runtime.code_home() / 'config.toml'))
        self.start([self.job['executable'], 'acp'])
        initialized = self.response(self.request('initialize', {
            'protocolVersion': 1, 'clientInfo': {'name': 'ccloop', 'version': '1'},
            'clientCapabilities': {}}))
        if initialized.get('protocolVersion') != 1:
            raise DriverError('Kimi ACP did not negotiate protocol version 1', 'provider')
        session = self.response(self.request('session/new', {'cwd': self.job['project'], 'mcpServers': []}))
        session_id = session.get('sessionId')
        if not isinstance(session_id, str) or not session_id:
            raise DriverError('Kimi ACP did not return a session ID', 'provider')
        self.details['session_id'] = session_id
        self.acp_configuration(session.get('configOptions'))
        self.response(self.request('session/set_model', {'sessionId': session_id, 'modelId': self.job['model']}))
        self.acp_model_selected = True
        self.acp_configuration(self.acp_config)
        # Code's default mode maps to manual permissions. Never inherit an
        # auto/yolo mode even when the optional picker metadata is absent.
        self.response(self.request('session/set_mode', {'sessionId': session_id, 'modeId': 'default'}))
        thinking = next((option for option in self.acp_config if option.get('id') == 'thinking'
                         and option.get('type') == 'select'), None)
        if thinking:
            values = []
            for option in thinking.get('options', []):
                if isinstance(option, dict):
                    values.extend(child.get('value') for child in option.get('options', [option])
                                  if isinstance(child, dict))
            effort = next((level for level in ('max', 'xhigh', 'high', 'medium', 'low', 'minimal', 'on')
                           if level in values), None)
            if effort:
                configured = self.response(self.request('session/set_config_option', {
                    'sessionId': session_id, 'configId': thinking['id'], 'value': effort}))
                self.acp_configuration(configured.get('configOptions'))
                self.details['requested_thinking'] = effort
        self.details['work_started'] = True
        self.end = time.monotonic() + self.job['timeout_seconds']
        result = self.response(self.request('session/prompt', {
            'sessionId': session_id, 'prompt': [{'type': 'text', 'text': self.job['prompt']}]}))
        stop = result.get('stopReason')
        if stop != 'end_turn':
            raise DriverError('Kimi ACP turn did not finish: ' + str(stop),
                              'cancelled' if stop == 'cancelled' else 'uncertain_work')
        return json.loads(providers.strip_fence(self.details.get('text', '')))

    def claude(self):
        self.start([self.job['executable'], '--print', '--input-format', 'text',
                    '--output-format', 'stream-json', '--verbose', '--model', self.job['model'], '--effort', 'max',
                    '--permission-prompts', 'none', '--json-schema', json.dumps(self.job['report_schema'])])
        self.details['work_started'] = True
        self.end = time.monotonic() + self.job['timeout_seconds']
        self.send_bytes(self.job['prompt'].encode())
        self.process.stdin.close()
        while 'claude_result' not in self.details:
            self.receive()
        value = self.details.pop('claude_result')
        self.err.flush()
        diagnostics = (self.directory / 'stderr.txt').read_text(errors='replace')[-8000:]
        self.details['observed_models'] = sorted(value.get('modelUsage') or {})
        self.details['session_id'] = value.get('session_id')
        if value.get('is_error'):
            error = str(value.get('result') or diagnostics or 'Claude result reported an error')
            raise DriverError(error, 'access' if providers.ACCESS.search(error) else 'provider')
        if any(model != self.job['model'] for model in self.details['observed_models']):
            raise DriverError('Claude served a different model than requested', 'access')
        result = value.get('structured_output', value.get('result'))
        return json.loads(result) if isinstance(result, str) else result

    def close(self, completed=False):
        if self.process:
            if self.acp and not completed and self.details['session_id']:
                self.acp_shutdown_send({'jsonrpc': '2.0', 'method': 'session/cancel',
                                        'params': {'sessionId': self.details['session_id']}})
            # EOF is the normal RPC-server shutdown. Never accept a server that
            # exits unsuccessfully after emitting a purported completed turn.
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            end = time.monotonic() + (5 if completed else .3)
            while self.process.poll() is None and time.monotonic() < end:
                # Drain late output so a server can complete its normal EOF shutdown.
                if self.selector.select(.1):
                    data = os.read(self.process.stdout.fileno(), 65536)
                    if data:
                        self.buffer += data
                while b'\n' in self.buffer:
                    line, self.buffer = self.buffer.split(b'\n', 1)
                    try:
                        self.audit(json.loads(line))
                    except (ValueError, UnicodeError):
                        # Unparseable trailing output is not an audit source.
                        pass
                if completed:
                    try:
                        self.check()
                    except DriverError:
                        break
            if self.process.poll() is None:
                self.details['native_shutdown'] = 'forced; normal exit was not established'
            providers.terminate_group(self.process)
            self.details['native_returncode'] = self.process.returncode
            self.details['unexpected_exit'] = self.process.returncode != 0
            for stream in (self.process.stdin, self.process.stdout):
                if stream and not stream.closed:
                    stream.close()
        self.selector.close()
        for stream in (self.err, self.log):
            if stream:
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()


def supervise(path, owner_fd):
    job = private_json(path)
    directory = path.parent
    native = Native(job, directory, owner_fd)
    result = {'binding': job['binding'], 'status': 'failed', 'started_at': time.time()}
    def interrupted(signum, frame):
        raise DriverError('Supervisor interrupted; work may be incomplete', 'uncertain_work')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        report = getattr(native, job['driver'])()
        result.update(status='completed', report=report)
    except (DriverError, OSError, ValueError, KeyError, TypeError) as error:
        result.update(error=str(error), failure_kind=getattr(error, 'kind', 'invalid'))
    finally:
        native.close(completed=result['status'] == 'completed')
    if native.details.get('unexpected_exit') and result['status'] == 'completed':
        result.update(status='failed', failure_kind='provider', error='Native process exited unsuccessfully after its result')
    if result['status'] == 'completed':
        try:
            result['source_snapshot'] = job['artifact_snapshot']
            result['artifact_snapshot'] = checkpoints.safe_snapshot(private_json(Path(job['root']) / 'state.json', 64_000_000))
        except (ValueError, OSError) as error:
            result.update(status='failed', failure_kind='uncertain_work', error=str(error))
    result.update(native.details, finished_at=time.time(), native_events=native.events[-20:])
    # Written independently of the manager. A restart may consume this result once.
    control.atomic_json(directory / 'result.json', public_audit(result))
    return 0


if __name__ == '__main__':
    sys.exit(supervise(Path(sys.argv[1]), int(sys.argv[2])))

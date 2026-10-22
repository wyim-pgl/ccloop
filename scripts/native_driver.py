"""Native work adapters, separate from the isolated, tool-free reviewers.

Codex protocol fields follow the CLI's generated app-server JSON schemas.
Kimi uses Wire instead of print mode, which implicitly auto-approves tools.
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


MAX_RESULT = 2_000_000


def private_reasoning(value):
    return (isinstance(value, dict) and ((isinstance(value.get('type'), str)
            and value['type'] in {'reasoning', 'thinking', 'redacted_thinking', 'think'})
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
            error = json.dumps(value['error'], ensure_ascii=False)
            raise DriverError(error, 'access' if providers.ACCESS.search(error) else 'provider')
        return value.get('result', {})

    def approval(self, value):
        """Only a person's explicit per-operation answer can approve a native request."""
        method = value['method']
        p = value.get('params', {})
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

    def event(self, value):
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

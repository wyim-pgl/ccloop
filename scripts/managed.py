"""Active manager for bounded native turns and durable context restoration."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

import ccloop
import checkpoints
import control
import native_driver
import providers


def report_schema(binding):
    text = {'type': 'string'}
    strings = {'type': 'array', 'items': text}
    def obj(fields):
        return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}
    return obj({'binding': obj({key: {'const': value} for key, value in binding.items()}),
                'summary': text, 'next_steps': strings, 'completed': strings, 'working_notes': text,
                'status': {'enum': ['continue', 'question', 'complete', 'blocked']}, 'question': text,
                'checks': {'type': 'array', 'items': obj({'method': text, 'result': text, 'evidence': text})}})


def validate_report(value, binding):
    ccloop.require(isinstance(value, dict) and set(value) == set(report_schema(binding)['required']),
                   'Worker must return exactly the requested report fields')
    ccloop.require(value['binding'] == binding, 'Worker report does not match the originating run and turn')
    ccloop.require(value['status'] in {'continue', 'question', 'complete', 'blocked'}, 'Unknown worker status')
    for key in ('summary', 'working_notes', 'question'):
        ccloop.require(isinstance(value[key], str), 'Invalid worker ' + key)
    ccloop.require(value['summary'].strip(), 'Worker summary is empty')
    ccloop.require(value['status'] != 'question' or value['question'].strip(), 'Worker question is empty')
    for key in ('next_steps', 'completed'):
        ccloop.require(isinstance(value[key], list) and all(isinstance(x, str) for x in value[key]),
                       'Invalid worker ' + key)
    ccloop.require(isinstance(value['checks'], list) and all(isinstance(x, dict)
                   and set(x) == {'method', 'result', 'evidence'}
                   and all(isinstance(v, str) and v.strip() for v in x.values()) for x in value['checks']),
                   'Invalid worker checks')
    ccloop.require(len(json.dumps(value).encode()) <= 100_000, 'Worker report exceeds checkpoint bounds')
    return value


@contextmanager
def manager_lock(root):
    fd = os.open(root / 'manager.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        ccloop.require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'Invalid manager lock')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another managed runtime owns this ledger') from None
        yield


def executable(driver):
    candidate = os.environ.get('CCLOOP_' + driver.upper() + '_BIN', driver)
    found = shutil.which(os.path.expanduser(candidate))
    return str(Path(found).absolute()) if found else None


def runnable(state):
    ccloop.require(state['enabled'] and state['phase'] == 'ready',
                   f"Ledger is {state['phase']}: {state.get('reason')}; reconcile explicitly before continuing")
    ccloop.require(not state.get('pending'), 'A peer consultation is in flight; reconcile it before continuing')
    ccloop.require(not ccloop.limit(state), 'Original run limit reached: ' + str(ccloop.limit(state)))


def halt(root, reason, kind, expected_generation=None):
    with ccloop.locked(root):
        state = ccloop.read(root)
        if (state['enabled'] and state['phase'] != 'complete'
                and (expected_generation is None or state['generation'] == expected_generation)):
            ccloop.halt(state, reason, kind)
            ccloop.commit(root, state, 'managed_blocked', reason=reason, failure_kind=kind)
    return state


def observed_files(state, before=None):
    """Supply bounded actual file content; worker check claims stay clearly labelled."""
    snapshot = checkpoints.safe_snapshot(state)
    result, remaining = [], 32_000
    project = Path(state['config']['project'])
    previous = (before or {}).get('files', {})
    paths = sorted(snapshot['files'], key=lambda path: (snapshot['files'][path] == previous.get(path), path))
    for relative in paths:
        digest = snapshot['files'][relative]
        if remaining <= 0 or len(result) >= 30:
            break
        path = project / relative
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            ccloop.require(stat.S_ISREG(info.st_mode), 'Review artifact is not a regular file')
            data = stream.read(min(remaining, 8000))
        remaining -= len(data)
        result.append({'path': relative, 'sha256': digest, 'text': data.decode('utf-8', errors='replace'),
                       'truncated': info.st_size > len(data)})
    ccloop.require(checkpoints.safe_snapshot(state) == snapshot, 'Artifacts changed while collecting review evidence')
    omitted = [path for path in paths if path not in {item['path'] for item in result}]
    return result, {'watched_count': len(paths), 'supplied_count': len(result),
                    'omitted_count': len(omitted), 'omitted_paths_excerpt': omitted[:100],
                    'deleted_paths_excerpt': sorted(set(previous) - set(paths))[:100],
                    'notice': 'Changed files are prioritized. Omitted files are not verified by this packet.'}


def evidence(state, outcome=None):
    files, coverage = observed_files(state, (outcome or {}).get('source_snapshot'))
    value = {'observed_files': files, 'coverage': coverage,
             'notice': 'Driver checks below are reports, not independent proof. Actual file excerpts and native events are supplied separately.'}
    if outcome:
        value['driver_reported'] = outcome['report']
        value['native_provenance'] = {key: outcome.get(key) for key in
            ('binding', 'requested_model', 'selected_model', 'observed_models', 'native_returncode', 'finished_at')}
        events, omitted = event_evidence(outcome.get('native_events', []))
        value['native_events_excerpt'] = json.dumps(events, ensure_ascii=False)
        value['native_events_omitted_count'] = omitted
    return value


def event_evidence(events):
    def bounded(value):
        if isinstance(value, str) and len(value) > 3000:
            return {'truncated': True, 'sha256': control.digest(value), 'characters': len(value),
                    'head': value[:1000], 'tail': value[-1500:]}
        if isinstance(value, dict):
            return {key: bounded(child) for key, child in value.items()}
        if isinstance(value, list):
            return [bounded(child) for child in value]
        return value
    selected, budget = [], 24_000
    for item in reversed(events):
        item = bounded(item)
        size = len(json.dumps(item, ensure_ascii=False).encode())
        if size <= budget:
            selected.append(item)
            budget -= size
    return list(reversed(selected)), len(events) - len(selected)


def initialize(args, models, overrides):
    directory = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state')) / 'ccloop' / 'runs'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='managed-', dir=directory))
    config = {'project': str(args.project), 'driver': args.driver, 'goal': args.goal,
              'authorization': 'Carry out this user-authorized goal within normal native permissions: ' + args.goal,
              'success_criteria': [{'id': 'original_goal', 'description': 'Verify the original goal is fully achieved: ' + args.goal}],
              'watch': ['**/*'], 'watch_exclude': ['graft/.cache/**', 'graft/.graph/**'],
              'models': models, 'auto_update_models': not bool(overrides)}
    control.atomic_json(root / 'initial-config.json', config)
    ccloop.initialize(root, root / 'initial-config.json')
    return root


def work_prompt(state, bundle, binding):
    prompt = json.dumps({
        'instructions': 'Act as a bounded native worker for this existing authorized ccloop run. '
            'Read all recovery file contents in context, including quarantine.md, whose entries must not be reused or retried without new evidence. Complete one coherent work step, '
            'then return only the JSON report matching report_schema. Preserve the original goal, '
            'criteria, authorization, and all budgets. Work only within normal native permissions. '
            'The manager owns the ledger, reviews, questions, and session rotation. Do not invoke '
            'ccloop, start another manager, modify its private run files, or make a final completion '
            'claim to the user. Do not leave background tools or commands running. For any ordinary '
            'technical or preference question, finish the step safely and return status=question '
            'with the question; the manager will consult the opposite provider. Permission or '
            'credential blockers may use status=blocked with a precise explanation. Use '
            'status=complete only to request independent final review. List actual executed '
            'checks honestly; never invent checks or results. Include explicit pending work in '
            'next_steps and durable implementation facts in working_notes. Echo binding exactly.',
        'binding': binding, 'context': bundle,
        'report_schema': report_schema(binding)}, ensure_ascii=False)
    ccloop.require(len(prompt.encode()) <= 96 * 1024, 'Recovery prompt exceeds 96 KiB; the original contract was not truncated')
    return prompt


def begin_turn(root, args):
    with ccloop.locked(root):
        state = ccloop.read(root)
        runnable(state)
        manager = state.setdefault('managed', {})
        ccloop.require(not manager.get('pending') and not manager.get('action'), 'Managed work is already pending')
        bundle = checkpoints.bundle(root, state)
        snapshot = checkpoints.safe_snapshot(state)
        turn_id = secrets.token_hex(12)
        directory = root / 'managed' / turn_id
        directory.mkdir(mode=0o700, parents=True)
        binding = {'run_id': state['run_id'], 'turn_id': turn_id, 'origin_revision': state['revision'],
                   'generation': state['generation'], 'artifact_sha256': snapshot['sha256']}
        driver = state['active_driver']
        job = {'root': str(root), 'project': state['config']['project'], 'driver': driver,
               'model': state.get('active_models', state['config']['models'])[driver],
               'executable': executable(driver), 'binding': binding,
               'artifact_snapshot': snapshot,
               'expected_revision': state['revision'] + 1,
               'owner_pid': os.getpid(), 'owner_identity': providers.process_identity(os.getpid()),
               'timeout_seconds': state['config']['timeout_seconds'],
               'access_timeout': args.access_timeout,
               'session_id': manager.get('session_id') if driver == 'codex' else None,
               'report_schema': report_schema(binding), 'prompt': work_prompt(state, bundle, binding)}
        ccloop.require(job['executable'], 'Native driver executable disappeared before work')
        control.atomic_json(directory / 'job.json', job)
        manager['pending'] = {'id': turn_id, 'binding': binding, 'directory': str(directory),
                              'expected_revision': state['revision'] + 1}
        ccloop.commit(root, state, 'managed_turn_intent', turn_id=turn_id,
                      artifact_sha256=snapshot['sha256'], context_digest=bundle['digest'])
    return directory, job


def answer_approval(directory, seen):
    path = directory / 'approval.json'
    if not path.exists():
        return
    request = native_driver.private_json(path)
    if seen.get(request['id']) == 'answered':
        return
    approved = False
    if sys.stdin.isatty():
        if request['id'] not in seen:
            print('Native permission request: ' + json.dumps(request['native'], ensure_ascii=False), file=sys.stderr)
            print('Approve this operation? Type yes to approve: ', end='', flush=True, file=sys.stderr)
            seen[request['id']] = {'input': ''}
        if not select.select([sys.stdin], [], [], 0)[0]:
            return
        # A raw/noncanonical terminal can become readable before a full line.
        # Read available bytes only, returning to the owner's stop/deadline poll.
        data = os.read(sys.stdin.fileno(), 4096)
        response = seen[request['id']]
        response['input'] += data.decode(errors='replace')
        if data and '\n' not in response['input'] and '\r' not in response['input']:
            return
        approved = response['input'].splitlines()[0].strip().lower() == 'yes' if response['input'] else False
    control.atomic_json(directory / 'approval-answer.json', {'id': request['id'], 'approve': approved})
    seen[request['id']] = 'answered'


def execute(root, directory, job):
    process = None
    seen = {}
    owner_read, owner_write = os.pipe()
    try:
        with open(directory / 'supervisor.stdout', 'wb') as stdout, open(directory / 'supervisor.stderr', 'wb') as stderr:
            process = subprocess.Popen([sys.executable, str(Path(native_driver.__file__).resolve()),
                str(directory / 'job.json'), str(owner_read)], stdout=stdout, stderr=stderr,
                start_new_session=True, pass_fds=(owner_read,))
            os.close(owner_read)
            owner_read = None
            control.atomic_json(directory / 'supervisor.json', {'pid': process.pid,
                'process_identity': providers.process_identity(process.pid), 'binding': job['binding']})
            while process.poll() is None:
                state = ccloop.read(root)
                if not state['enabled'] or state['generation'] != job['binding']['generation']:
                    process.terminate()
                    break
                answer_approval(directory, seen)
                time.sleep(.1)
            process.wait(timeout=5)
    finally:
        if owner_read is not None:
            os.close(owner_read)
        os.close(owner_write)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                providers.terminate_group(process)


def pending_result(root, state):
    pending = state['managed']['pending']
    directory = root / 'managed' / pending['id']
    ccloop.require(str(directory) == pending['directory'], 'Managed result directory identity mismatch')
    result_path = directory / 'result.json'
    if not result_path.exists():
        # A surviving supervisor may still be stopping an orphan. Never start a replacement.
        for filename in ('supervisor.json', 'native.json'):
            path = directory / filename
            if path.exists():
                saved = native_driver.private_json(path)
                if native_driver.alive(saved['pid'], saved['process_identity']):
                    try:
                        os.killpg(saved['pid'], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        raise native_driver.DriverError('Prior work has no durable completion result; reconcile its effects before resuming', 'uncertain_work')
    result = native_driver.private_json(result_path)
    ccloop.require(result.get('binding') == pending['binding'], 'Supervisor result identity mismatch')
    ccloop.require(state['generation'] == pending['binding']['generation']
                   and state['run_id'] == pending['binding']['run_id']
                   and state['revision'] == pending['expected_revision'], 'Work result is stale or superseded')
    if result.get('status') != 'completed':
        kind = result.get('failure_kind', 'uncertain_work')
        # Access before any work is safe to hand off. An access failure after dispatch
        # may include commands or edits and therefore cannot be automatically repeated.
        if kind == 'access' and result.get('work_started'):
            kind = 'uncertain_work'
        raise native_driver.DriverError(result.get('error', 'Native work did not complete'), kind)
    ccloop.require(result.get('native_returncode') == 0, 'Native worker did not exit successfully')
    validate_report(result.get('report'), pending['binding'])
    ccloop.require(result.get('artifact_snapshot') == checkpoints.safe_snapshot(state),
                   'Artifacts changed after native work completed; reconcile before continuing')
    return result, directory


def accept_result(root, state, result, directory, threshold):
    report = result['report']
    packet = {key: report[key] for key in ('summary', 'next_steps', 'completed', 'working_notes')}
    control.atomic_json(directory / 'checkpoint.json', packet)
    # Preserve an integration marker before the checkpoint commit, so a process
    # death between these two durable writes cannot repeat edits or lose the report.
    with ccloop.locked(root):
        current = ccloop.read(root)
        integration_guard(current, result)
        ccloop.require(current['revision'] == state['revision'], 'Ledger changed before work acceptance')
        current['managed']['pending']['integrating'] = True
        ccloop.commit(root, current, 'managed_turn_result', turn_id=report['binding']['turn_id'],
                      driver_reported={key: value for key, value in report.items() if key != 'binding'},
                      native_provenance={key: result.get(key) for key in
                          ('binding', 'requested_model', 'selected_model', 'observed_models', 'native_returncode', 'finished_at')})
    ccloop.checkpoint(root, directory / 'checkpoint.json', expected_managed={
        'binding': result['binding'], 'artifact_snapshot': result['artifact_snapshot']})
    finish_acceptance(root, result, threshold)


def finish_acceptance(root, result, threshold):
    report = result['report']
    with ccloop.locked(root):
        state = ccloop.read(root)
        integration_guard(state, result, integrating=True)
        manager = state['managed']
        usage = result.get('context_usage') if state['active_driver'] == 'codex' else None
        measured = isinstance(usage, dict) and type(usage.get('tokens')) is int and type(usage.get('window')) is int and usage['window'] > 0
        marker = root / 'managed' / ccloop.ROTATE_MARKER
        requested = control.load(marker) if marker.is_file() and not marker.is_symlink() else None
        requested = requested if isinstance(requested, dict) and requested.get('run_id') == state['run_id'] else None
        rotate = bool(requested) or not measured or usage['tokens'] / usage['window'] >= threshold
        manager.update(pending=None, turn_count=manager.get('turn_count', 0) + 1,
            session_id=None if rotate else result.get('session_id'), last_turn=report['binding']['turn_id'],
            last_native={key: result.get(key) for key in ('requested_model', 'selected_model', 'observed_models', 'context_usage', 'session_id')},
            context_policy='measured-current-context' if measured else 'fresh-per-turn-no-measurement')
        if report['status'] in {'question', 'complete', 'blocked'}:
            manager['action'] = {'id': report['binding']['turn_id'], 'kind': report['status'],
                                 'after_round': state['round'], 'directory': str(root / 'managed' / report['binding']['turn_id'])}
        ccloop.commit(root, state, 'managed_turn_accepted', turn_id=report['binding']['turn_id'],
                      rotate=rotate, context_policy=manager['context_policy'],
                      **({'rotate_requested': requested['reason']} if requested else {}))
        if marker.exists():
            marker.unlink()
    print('ccloop managed: ' + manager['context_policy'] + ('; next turn starts fresh' if rotate else '; retaining native thread'), file=sys.stderr)


def recover_integration(root, state, threshold):
    pending = state['managed']['pending']
    directory = root / 'managed' / pending['id']
    result = native_driver.private_json(directory / 'result.json')
    ccloop.require(result.get('status') == 'completed' and result.get('binding') == pending['binding'],
                   'Invalid work result during checkpoint recovery')
    validate_report(result['report'], pending['binding'])
    integration_guard(state, result, integrating=True)
    # Re-saving notes is idempotent with respect to work; no native command runs here.
    ccloop.checkpoint(root, directory / 'checkpoint.json', expected_managed={
        'binding': result['binding'], 'artifact_snapshot': result['artifact_snapshot']})
    finish_acceptance(root, result, threshold)


def integration_guard(state, result, integrating=False):
    runnable(state)
    binding = result['binding']
    pending = state.get('managed', {}).get('pending') or {}
    ccloop.require(pending.get('binding') == binding and state['run_id'] == binding['run_id']
                   and state['generation'] == binding['generation'], 'Native result was superseded before acceptance')
    ccloop.require(not integrating or pending.get('integrating') is True, 'Native result is not being integrated')
    ccloop.require(result.get('status') == 'completed' and result.get('native_returncode') == 0,
                   'Native worker did not exit successfully')
    ccloop.require(checkpoints.safe_snapshot(state) == result.get('artifact_snapshot'),
                   'Artifacts changed after native work completed; reconcile before continuing')


def perform_action(root, action):
    state = ccloop.read(root)
    if action['kind'] == 'blocked':
        result = native_driver.private_json(Path(action['directory']) / 'result.json')
        halt(root, result['report']['summary'], 'worker_blocker')
        return
    # If the manager died after the peer result committed, consume that accepted
    # result instead of paying for/replaying a second consultation.
    expected_round = action['after_round'] + 1
    accepted = next((event for event in state['history'] if event['round'] == expected_round
                     and event['action'] in {'review', 'decision'}), None)
    if accepted is None:
        runnable(state)
        ccloop.require(state['round'] == action['after_round'], 'Peer action changed; reconcile it explicitly')
        outcome = native_driver.private_json(Path(action['directory']) / 'result.json') if action.get('directory') else None
        packet = evidence(state, outcome)
        packet['managed_action_id'] = action['id']
        path = root / 'managed' / ('action-' + action['id'] + '.json')
        control.atomic_json(path, packet)
        if action['kind'] == 'question':
            state, ok = ccloop.ask(root, outcome['report']['question'], path)
        else:
            stage = 'final' if action['kind'] == 'complete' else action['kind']
            state, ok = ccloop.consult(root, path, stage)
        if not ok:
            return
    with ccloop.locked(root):
        state = ccloop.read(root)
        if (state.get('managed', {}).get('action') or {}).get('id') == action['id']:
            state['managed']['action'] = None
            ccloop.commit(root, state, 'managed_peer_consumed', action_id=action['id'])


def schedule_review(root, state):
    with ccloop.locked(root):
        current = ccloop.read(root)
        runnable(current)
        ccloop.require(current['revision'] == state['revision'], 'Ledger changed before review scheduling')
        manager = current.setdefault('managed', {})
        manager['action'] = {'id': secrets.token_hex(12), 'kind': 'plan' if not current.get('last_review') else 'milestone',
                             'after_round': current['round'],
                             'directory': str(root / 'managed' / manager['last_turn']) if manager.get('last_turn') else None}
        (root / 'managed').mkdir(mode=0o700, exist_ok=True)
        ccloop.commit(root, current, 'managed_peer_intent', action_id=manager['action']['id'])


def fallback(root, state, reason):
    ccloop.require(state['active_driver'] != 'kimi' and executable('kimi'), reason + '; Kimi is unavailable')
    ccloop.change_state(root, 'handoff', reason, driver='kimi', expected_generation=state['generation'])
    with ccloop.locked(root):
        state = ccloop.read(root)
        runnable(state)
        state.setdefault('managed', {}).update(pending=None, session_id=None)
        ccloop.commit(root, state, 'managed_access_fallback', reason=reason)
    print('ccloop managed: ' + reason + '; using Kimi with an independent peer', file=sys.stderr)


def readiness(root, state, timeout):
    """Probe access without project tools before any work can have side effects."""
    driver = state['active_driver']
    models = state.get('active_models', state['config']['models'])
    with ccloop.locked(root):
        current = ccloop.read(root)
        runnable(current)
        ccloop.require(current['revision'] == state['revision'], 'Run changed before native readiness')
        identifier = secrets.token_hex(12)
        directory = root / 'managed' / ('readiness-' + identifier)
        directory.mkdir(mode=0o700, parents=True)
        ccloop.commit(root, current, 'managed_readiness_intent', probe_id=identifier,
                      provider=driver, requested_model=models[driver])
        generation, run_id = current['generation'], current['run_id']

    def cancelled():
        current = ccloop.read(root)
        return (not current['enabled'] or current['phase'] != 'ready' or current.get('pending')
                or current['generation'] != generation or current['run_id'] != run_id
                or ccloop.deadline_expired(current))

    attempt, report = providers.invoke(driver, {'models': models, 'commands': {driver: [executable(driver)]}},
        'This is a tool-free model access readiness check. Do not execute a task, read files, '
        'call tools, or invoke any agent or CLI. Return only the JSON object {"status":"ready"}.',
        directory, min(timeout, max(.001, ccloop.remaining_seconds(current) or timeout)),
        cancelled=cancelled, started=lambda details: not cancelled())
    if attempt['status'] == 'received' and report != {'status': 'ready'}:
        attempt.update(status='invalid', failure_kind='invalid', error='Readiness probe did not return exactly {"status":"ready"}')
    with ccloop.locked(root):
        current = ccloop.read(root)
        ccloop.require(not cancelled(), 'Run stopped or changed during readiness')
        current.setdefault('managed', {})['last_readiness'] = attempt
        ccloop.commit(root, current, 'managed_readiness_result', probe_id=identifier, attempt=attempt)
    if attempt['status'] != 'received':
        raise native_driver.DriverError(attempt.get('error', 'Native access readiness failed'), attempt.get('failure_kind', 'invalid'))


def reconcile(root, packet_path, reason, expected_turn):
    """Retire explicitly inspected uncertain work without replaying it or resuming."""
    root = Path(root).expanduser().resolve()
    ccloop.require(not os.environ.get('CCLOOP_MANAGED_WORKER') and not os.environ.get('CCLOOP_CONSULTANT')
                   and not os.environ.get('CCLOOP_REVIEWER'), 'Workers and consultants cannot reconcile managed work')
    ccloop.require(isinstance(reason, str) and reason.strip(), 'Managed reconciliation requires an inspection reason')
    packet = native_driver.private_json(packet_path, 128 * 1024)
    ccloop.require(isinstance(packet, dict) and set(packet) == {'summary', 'next_steps', 'completed', 'working_notes', 'checks'},
                   'Reconciliation packet needs summary, next_steps, completed, working_notes, checks')
    ccloop.require(isinstance(packet['checks'], list) and packet['checks'] and all(isinstance(check, dict)
                   and set(check) == {'method', 'result', 'evidence'} and all(control.nonempty(v) for v in check.values())
                   for check in packet['checks']), 'Reconciliation requires actual inspection checks with evidence')
    with manager_lock(root):
        with ccloop.locked(root):
            state = ccloop.read(root)
            ccloop.require(not state['enabled'] and state['phase'] in {'stopped', 'blocked'},
                           'Stop the run before reconciling managed work')
            ccloop.require(not state.get('pending'), 'Reconcile the in-flight peer consultation first')
            manager = state.get('managed', {})
            record = manager.get('pending') or manager.get('action')
            ccloop.require(record and record.get('id') == expected_turn, 'Managed reconciliation turn does not match the pending nonce')
            directory = root / 'managed' / expected_turn
            for name in ('supervisor.json', 'native.json'):
                path = directory / name
                if path.exists():
                    process = native_driver.private_json(path)
                    ccloop.require(not ccloop.process_alive(process['pid'], process.get('process_identity')),
                                   'Recorded native work is still alive; wait for it to stop before reconciling')
            snapshot = checkpoints.safe_snapshot(state)
            generation = state['generation']
            note_path = root / 'managed' / ('reconciled-' + expected_turn + '.json')
            control.atomic_json(note_path, {key: value for key, value in packet.items() if key != 'checks'})
        ccloop.checkpoint(root, note_path)
        with ccloop.locked(root):
            current = ccloop.read(root)
            ccloop.require(not current['enabled'] and current['phase'] in {'stopped', 'blocked'}
                           and current['generation'] == generation, 'Run changed during managed reconciliation')
            manager = current['managed']
            record = manager.get('pending') or manager.get('action')
            ccloop.require(record and record['id'] == expected_turn and checkpoints.safe_snapshot(current) == snapshot,
                           'Managed work or artifacts changed during reconciliation')
            for field in ('pending', 'action'):
                if (manager.get(field) or {}).get('id') == expected_turn:
                    manager[field] = None
            manager['session_id'] = None
            ccloop.commit(root, current, 'managed_reconcile', turn_id=expected_turn, reason=reason,
                          checks=packet['checks'], artifact_snapshot=snapshot)
    return current


def run(args, models, root, overrides):
    previous = os.environ.get('CCLOOP_MODEL_METADATA_OFFLINE')
    if getattr(args, 'offline_models', False):
        os.environ['CCLOOP_MODEL_METADATA_OFFLINE'] = '1'
    try:
        return _run(args, models, root, overrides)
    finally:
        if previous is None:
            os.environ.pop('CCLOOP_MODEL_METADATA_OFFLINE', None)
        else:
            os.environ['CCLOOP_MODEL_METADATA_OFFLINE'] = previous


def _run(args, models, root, overrides):
    ccloop.require(not os.environ.get('CCLOOP_MANAGED_WORKER'), 'A managed worker cannot start another manager')
    ccloop.require(math.isfinite(args.context_threshold) and 0 < args.context_threshold < 1,
                   'context-threshold must be between zero and one')
    root = Path(root).expanduser().resolve() if root else initialize(args, models, overrides)
    print('ccloop managed root: ' + str(root), file=sys.stderr)
    can_halt = False
    try:
        with manager_lock(root):
            state = ccloop.read(root)
            ccloop.require(state['config']['goal'] == args.goal and Path(state['config']['project']).resolve() == args.project.resolve(),
                           'Goal/project differs from the frozen run')
            saved_models = state.get('active_models', state['config']['models'])
            ccloop.require(all(saved_models[key] == value for key, value in overrides.items()), 'Model override differs from saved run')
            if state['phase'] == 'complete':
                return 0
            runnable(state)
            ccloop.require(state['active_driver'] == args.driver, 'Another driver owns this run; use the saved driver or explicitly hand off')
            can_halt = True
            checked = set()
            active_driver = args.driver
            while True:
                state = ccloop.read(root)
                if state['phase'] == 'complete':
                    print('ccloop managed complete; journal: ' + str(root / 'ccloop.md'))
                    return 0
                runnable(state)
                ccloop.require(state['active_driver'] == active_driver, 'Managed driver was externally superseded')
                manager = state.get('managed', {})
                pending = manager.get('pending')
                if pending:
                    if pending.get('integrating'):
                        recover_integration(root, state, args.context_threshold)
                    else:
                        try:
                            result, directory = pending_result(root, state)
                        except native_driver.DriverError as error:
                            if error.kind == 'access' and state['active_driver'] != 'kimi':
                                fallback(root, state, str(error))
                                active_driver = 'kimi'
                                continue
                            raise
                        accept_result(root, state, result, directory, args.context_threshold)
                    continue
                if manager.get('action'):
                    perform_action(root, manager['action'])
                    continue
                if not executable(state['active_driver']):
                    fallback(root, state, state['active_driver'] + ' executable is unavailable')
                    active_driver = 'kimi'
                    continue
                selected = state.get('active_models', state['config']['models'])[state['active_driver']]
                if (state['active_driver'], selected) not in checked:
                    try:
                        readiness(root, state, args.access_timeout)
                    except native_driver.DriverError as error:
                        if error.kind == 'access' and state['active_driver'] != 'kimi':
                            fallback(root, ccloop.read(root), str(error))
                            active_driver = 'kimi'
                            continue
                        raise
                    checked.add((state['active_driver'], selected))
                    continue
                if ccloop.summary(state)['review_due']:
                    schedule_review(root, state)
                    continue
                directory, job = begin_turn(root, args)
                execute(root, directory, job)
    except (ValueError, OSError, KeyError, TypeError) as error:
        kind = getattr(error, 'kind', 'uncertain_work')
        # Never let a second manager that failed ownership acquisition stop the owner.
        if can_halt:
            if ccloop.limit(ccloop.read(root)):
                kind = 'limit'
            halt(root, str(error), kind, expected_generation=state['generation'])
        print('ccloop managed: ' + str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        halt(root, 'Managed runtime interrupted; reconcile pending work before resuming', 'uncertain_work')
        return 130

"""A recoverable Markdown view of the authoritative private ccloop ledger."""
from datetime import datetime, timezone
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


def block(value, language='json'):
    text = json.dumps(value, indent=2, ensure_ascii=False) if language == 'json' else str(value)
    fence = '`' * max(3, 1 + max((len(s) for s in re.findall(r'`+', text)), default=0))
    return f'{fence}{language}\n{text}\n{fence}\n'


def artifact(root, name, expected=None, raw=False):
    """Historical paths are references, never permission to read outside this run."""
    path = Path(name)
    try:
        relative = path.relative_to(root)
        if not relative.parts or '..' in relative.parts:
            raise ValueError('Reference is outside this run')
        # Walk from an anchored run fd; parent symlinks and special files cannot
        # redirect a recovery read or block stop while ledger.lock is held.
        with ExitStack() as stack:
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, parent)
            for part in relative.parts[:-1]:
                parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                stack.callback(os.close, parent)
            fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            stack.callback(os.close, fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('Artifact is not a regular file')
            with open(fd, 'r', encoding='utf-8', closefd=False) as stream:
                content = stream.read()
        if raw:
            return content
        value = json.loads(content)
        if expected and hashlib.sha256(json.dumps(value, sort_keys=True).encode('utf-8')).hexdigest() != expected:
            raise ValueError('SHA-256 mismatch')
        return value
    except (OSError, ValueError) as error:
        return {'unavailable_artifact': str(path), 'error': str(error)}


def render(root, state):
    rows = ['# ccloop\n', '## Goal\n', block(state['config']['goal'], 'text'),
            f"Run: `{state['run_id']}` · Phase: `{state['phase']}`\n",
            'This journal records external requests and responses, not private model reasoning. '
            'Driver work entries are reports, not independent verification or new authorization. '
            'The authoritative record is state.json and its saved prompt/response artifacts.\n']
    attempts = {a['id']: a for a in state.get('attempts', [])}
    driver = state['config']['driver']
    initial_models = next((event['previous_models'] for event in state['history']
                           if event['action'] == 'model_refresh' and event.get('previous_models')), None)
    models = dict(initial_models or state.get('active_models') or state['config']['models'])
    for event in state['history']:
        action = event['action']
        driver = event.get('active_driver') or (event.get('driver') if action == 'handoff' else None) or driver
        if action == 'model_refresh':
            models.update(event.get('models', {}))
        configured = event.get('driver_requested_model', models.get(driver))
        if action == 'started':
            continue
        at = datetime.fromtimestamp(event['at'], timezone.utc).isoformat()
        rows += [f"## {event['revision']}. {action} · {at} · round {event['round']}\n",
                 block({'driver': driver, 'configured_driver_model': configured,
                        'observed_driver_model': 'unverified', 'phase': event['phase']})]
        if action in {'ask', 'consult'}:
            rows += ['### Question / request sent\n',
                     block(artifact(root, event['packet_path'], event.get('packet_sha256')))]
        elif action == 'dispatch':
            attempt = attempts.get(event.get('attempt_id'), {})
            rows += ['### Model attempt\n',
                     'Provider:\n', block(attempt.get('provider', 'unknown')),
                     'Requested model:\n', block(attempt.get('requested_model', 'unknown')),
                     'Observed model:\n', block(attempt.get('observed_models') or ['unverified']),
                     block({k: v for k, v in attempt.items() if k not in
                            {'argv', 'provider', 'requested_model', 'observed_models', 'raw_stdout'}})]
            if attempt.get('report_path') and attempt.get('status') != 'accepted':
                rows += ['### Unaccepted response\n', block(artifact(root, attempt['report_path']))]
            elif attempt.get('stdout_path') and attempt.get('status') not in {'dispatching', 'running', 'accepted'}:
                raw = attempt['raw_stdout'] if 'raw_stdout' in attempt else artifact(root, attempt['stdout_path'], raw=True)
                rows += ['### Unaccepted raw output\n', block(raw, 'text')]
        elif action in {'decision', 'review'}:
            rows += ['### Answer / review accepted\n', block(event[action])]
        elif action == 'work':
            rows += ['### Driver-reported choice, actions and checks\n', block(event['entry']),
                     block({k: event.get(k) for k in
                            ('consultation_round', 'consultation_attempt_id',
                             'consultation_artifact_sha256', 'artifact_sha256')})]
        else:
            detail = {k: v for k, v in event.items() if k not in
                      {'at', 'action', 'revision', 'round', 'phase', 'active_driver', 'driver_requested_model'}}
            if detail:
                rows.append(block(detail))
    return '\n'.join(rows)


def write(root, state):
    """Called under ledger.lock; replace the derived view only after state is durable."""
    content = render(root, state)
    fd, name = tempfile.mkstemp(prefix='.state-journal-', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
        # Authoritative state and JSON payloads are fsynced (failed raw replies
        # live in state). This view needs atomic visibility and is reconstructable.
        os.replace(name, root / 'ccloop.md')
    finally:
        if os.path.exists(name):
            os.unlink(name)

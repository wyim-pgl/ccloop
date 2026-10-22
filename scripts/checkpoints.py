"""Bounded recovery views of the authoritative private ccloop ledger.

Call write and bundle while holding ledger.lock. Mirrors are disposable: no
recovery path ever reads them, even when their checksums appear to be valid.
"""
from contextlib import contextmanager, ExitStack
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

import control


NAMES = ('resume.md', 'intend.md', 'todo.md', 'quarantine.md')
MAX_QUARANTINE_ENTRIES = 50
MANIFEST = 'checkpoints.json'
MAX_PACKET_BYTES = 128 * 1024
MAX_CONTRACT_BYTES = 24 * 1024
MAX_BUNDLE_BYTES = 64 * 1024


def encoded(value):
    # Keep all metadata and reported text inside JSON, including backtick runs
    # and Unicode line separators that could otherwise escape a Markdown fence.
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)
            .replace('`', '\\u0060').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def block(value, budget=None):
    text = encoded(value)
    if budget is not None and len(text.encode('utf-8')) > budget:
        # The excerpt is itself a JSON string, so truncation cannot leave an
        # open fence or turn the remaining text into instructions or headings.
        preview = text.encode('utf-8')[:max(0, (budget - 400) // 6)].decode('utf-8', errors='ignore')
        text = encoded({'omitted': 'Content truncated; full record remains in state.json.',
                        'sha256': sha256(text), 'utf8_bytes': len(text.encode('utf-8')),
                        'excerpt': preview})
    return '```json\n' + text + '\n```\n'


def identity(state):
    control.require(state.get('schema') == 'ccloop-1', 'Unsupported checkpoint ledger')
    control.require(isinstance(state.get('run_id'), str) and 0 < len(state['run_id']) <= 256,
                    'Invalid checkpoint run identity')
    for name in ('revision', 'generation'):
        control.require(type(state.get(name)) is int and state[name] >= 0,
                        f'Invalid checkpoint {name}')
    control.require(state.get('active_driver') in {'claude', 'codex', 'kimi'},
                    'Invalid checkpoint driver')
    return {key: state[key] for key in ('run_id', 'revision', 'generation', 'active_driver')}


def render(state, strict=False):
    stamp = block(identity(state))
    config = state['config']
    contract = encoded(config)
    oversized = len(contract.encode('utf-8')) > MAX_CONTRACT_BYTES
    control.require(not strict or not oversized,
                    'Frozen contract exceeds 24 KiB recovery limit; read the complete original '
                    'state.json config before continuing. No goal, authorization, or criteria were truncated.')
    contract_view = (block({'omitted': 'Frozen contract exceeds recovery limit. Do not execute from this view; '
                           'read the complete original state.json config.',
                           'sha256': sha256(contract), 'utf8_bytes': len(contract.encode('utf-8'))})
                     if oversized else block(config))
    intend = ('# Original intent\n\n' + stamp + '\n'
              'The complete frozen contract below is copied from state.json. Preserve its goal, '
              'authorization, success criteria and limits exactly. Recovery never starts a new run '
              'or resets the review clock. A user stop overrides continued work.\n\n'
              '## Frozen contract\n\n' + contract_view)
    if oversized:
        intend = intend.replace('The complete frozen contract below is copied from state.json.',
                                'The full frozen contract is too large for this view and remains in state.json.')

    checkpoint = state.get('driver_checkpoint') or {}
    review = state.get('last_review') or {}
    runtime_keys = ('enabled', 'phase', 'reason', 'failure_kind', 'round', 'created_at', 'deadline',
                    'no_progress_reviews', 'question_count', 'last_review_at', 'next_review_at', 'active_models')
    runtime = {key: state.get(key) for key in runtime_keys}
    runtime['next_review_at'] = state.get('next_review_at', state['created_at'])
    notes = {key: value for key, value in checkpoint.items() if key != 'snapshot'}
    last_work = next((event for event in reversed(state.get('history', []))
                      if event.get('action') == 'work'), None)
    artifacts = {'driver_checkpoint_sha256': checkpoint.get('artifact_sha256'),
                 'last_review_artifact_sha256': (state.get('review_snapshot') or {}).get('sha256'),
                 'last_review_sha256': state.get('review_sha256')}
    resume = ('# Resume the existing run\n\n' + stamp + '\n'
              'state.json is authoritative. Driver notes, completed items and peer decisions are '
              'reports, not independent verification or new authorization. Only accepted independent '
              'reviews establish criterion evidence; only the existing final review gate completes '
              'the goal. Reconcile saved artifact identities with current files before reusing evidence. '
              'Never replay an uncertain work step or an in-flight consultation.\n\n'
              '## Run status and unchanged timing\n\n' + block(runtime, 2200) + '\n'
              '## Accepted independent review\n\n' + block(review or None, 6000) + '\n'
              '## Accepted advisory decision\n\n' + block(state.get('last_decision'), 2500) + '\n'
              '## Driver-reported checkpoint, not independent verification\n\n' + block(notes or None, 5000) + '\n'
              '## Latest driver-reported work\n\n' + block(last_work, 1500) + '\n'
              '## Saved artifact identities\n\n' + block(artifacts, 1000) + '\n'
              '## Pending consultation\n\n' + block({key: value for key, value in (state.get('pending') or {}).items()
                                                        if key != 'snapshot'} or None, 1000))
    criteria = [{'id': criterion['id'], 'description': criterion['description'],
                 'review': (review.get('criteria') or {}).get(criterion['id'], {'status': 'unknown'})}
                for criterion in config['success_criteria']
                if (review.get('criteria') or {}).get(criterion['id'], {}).get('status') != 'met']
    issues = [issue for issue in review.get('issues', []) if issue.get('status') == 'open']
    todo = ('# Pending work for the original goal\n\n' + stamp + '\n'
            'Driver next steps are suggestions within the frozen authorization. Reported completed '
            'items do not satisfy criteria. Honor stopped, blocked, complete, exhausted, or busy state '
            'before taking any action. If a section is truncated, inspect its full state.json record.\n\n'
            '## Explicit driver next steps\n\n' + block(checkpoint.get('next_steps', []), 3500) + '\n'
            '## Criteria without accepted met evidence\n\n' + block(criteria, 3000) + '\n'
            '## Open reviewed issues\n\n' + block(issues, 2500) + '\n'
            '## Reviewer next milestone\n\n' + block(review.get('next_milestone'), 1000))
    refuted = [claim for claim in review.get('claims', []) if claim.get('status') == 'refuted']
    resolved = [issue for issue in review.get('issues', []) if issue.get('status') == 'resolved']
    quarantine = ('# Quarantine\n\n' + stamp + '\n'
                  'Entries below are superseded, refuted, abandoned or unverified. Do not reuse '
                  'them as facts or retry them without new evidence. Driver entries are reports; '
                  'review entries come from accepted independent reviews.\n\n'
                  '## Driver-reported quarantine\n\n' + block(state.get('quarantine') or [], 2500) + '\n'
                  '## Refuted review claims\n\n' + block(refuted, 1500) + '\n'
                  '## Resolved review issues\n\n' + block(resolved, 1200) + '\n'
                  '## Superseded driver checkpoint\n\n' + block(state.get('superseded_checkpoint'), 800))
    files = dict(zip(NAMES, (resume, intend, todo, quarantine)))
    control.require(sum(len(text.encode('utf-8')) for text in files.values()) <= MAX_BUNDLE_BYTES,
                    'Recovery bundle exceeds its bounded size')
    return files


def manifest(state, files):
    result = {'schema': 'ccloop-checkpoints-1', **identity(state),
              'files': {name: sha256(files[name]) for name in NAMES}}
    result['digest'] = sha256(json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=False))
    return result


def validate_checkpoint(state):
    checkpoint = state.get('driver_checkpoint')
    if checkpoint is None:
        return
    control.require(isinstance(checkpoint, dict), 'Invalid checkpoint provenance: expected a saved record')
    origin, revision, generation = (checkpoint.get(key) for key in
                                    ('originating_revision', 'revision', 'generation'))
    valid = (checkpoint.get('run_id') == state['run_id']
             and type(origin) is int and type(revision) is int and type(generation) is int
             and origin >= 0 and revision == origin + 1 and revision <= state['revision']
             and 0 <= generation <= state['generation'])
    snapshot = checkpoint.get('snapshot')
    valid = valid and isinstance(snapshot, dict) and isinstance(snapshot.get('files'), dict)
    control.require(valid, 'Invalid checkpoint provenance: run, revision, generation or snapshot mismatch')
    digest = control.digest(json.dumps(snapshot['files'], sort_keys=True))
    control.require(snapshot.get('sha256') == digest == checkpoint.get('artifact_sha256'),
                    'Invalid checkpoint provenance: artifact digest mismatch')
    event = next((event for event in state.get('history', [])
                  if event.get('action') == 'checkpoint' and event.get('revision') == revision), None)
    control.require(event is not None and event.get('originating_revision') == origin
                    and event.get('artifact_sha256') == digest,
                    'Invalid checkpoint provenance: durable checkpoint event mismatch')


@contextmanager
def opened_path(path, flags):
    """Anchor every path component without following parent or leaf symlinks."""
    path = Path(path).absolute()
    with ExitStack() as stack:
        parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, parent)
        for part in path.parts[1:-1]:
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, parent)
        fd = os.open(path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        stack.callback(os.close, fd)
        yield fd


def read_regular(path, maximum=None):
    """Do not follow symlinks, block on a FIFO, or over-read a packet."""
    with opened_path(path, os.O_RDONLY) as fd:
        with open(fd, 'rb', closefd=False) as stream:
            info = os.fstat(fd)
            control.require(stat.S_ISREG(info.st_mode), 'Recovery input must be a regular file')
            control.require(maximum is None or info.st_size <= maximum, 'Checkpoint packet exceeds 128 KiB')
            content = stream.read() if maximum is None else stream.read(maximum + 1)
            control.require(maximum is None or len(content) <= maximum, 'Checkpoint packet exceeds 128 KiB')
    try:
        return json.loads(content)
    except RecursionError:
        raise ValueError('Recovery JSON exceeds the supported nesting depth') from None


def safe_snapshot(state):
    """Match control.snapshot identities without following links or opening FIFOs."""
    files = {}
    excluded = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.mypy_cache', '.pytest_cache'}

    def walk_error(error):
        raise error

    def signature(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    with opened_path(state['config']['project'], os.O_RDONLY | os.O_DIRECTORY) as project:
        for folder, dirs, names, directory in os.fwalk('.', dir_fd=project, onerror=walk_error,
                                                       follow_symlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in excluded and not
                             stat.S_ISLNK(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode))
            for name in sorted(names):
                relative = (Path(folder) / name).as_posix()
                if not any(fnmatch.fnmatchcase(relative, pattern) or
                           (pattern.startswith('**/') and fnmatch.fnmatchcase(relative, pattern[3:]))
                           for pattern in state['config']['watch']):
                    continue
                if any(fnmatch.fnmatchcase(relative, pattern) or
                       (pattern.startswith('**/') and fnmatch.fnmatchcase(relative, pattern[3:]))
                       for pattern in state['config'].get('watch_exclude', [])):
                    continue
                control.require(len(files) < 10000, 'Too many watched files; narrow watch patterns')
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    control.require(stat.S_ISREG(before.st_mode),
                                    f'Watched artifact must be a regular file: {relative}')
                    control.require(before.st_size <= 50_000_000, f'Watched file too large: {relative}')
                    digest = hashlib.sha256()
                    length = 0
                    for chunk in iter(lambda: stream.read(1_048_576), b''):
                        length += len(chunk)
                        control.require(length <= 50_000_000, f'Watched file too large: {relative}')
                        digest.update(chunk)
                    after = os.fstat(stream.fileno())
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    control.require(signature(before) == signature(after) == signature(current),
                                    f'Artifact changed during snapshot: {relative}; retry after work settles')
                    files[relative] = digest.hexdigest()
    return {'sha256': control.digest(json.dumps(files, sort_keys=True)), 'files': files}


def quarantine_entries(value):
    """Normalize reported quarantine entries to statement/reason pairs."""
    control.require(isinstance(value, list) and len(value) <= MAX_QUARANTINE_ENTRIES,
                    f'Checkpoint quarantine must be a list of at most {MAX_QUARANTINE_ENTRIES} entries')
    entries = []
    for item in value:
        if isinstance(item, str):
            item = {'statement': item, 'reason': ''}
        control.require(isinstance(item, dict) and set(item) <= {'statement', 'reason'}
                        and control.nonempty(item.get('statement')) and isinstance(item.get('reason', ''), str),
                        'Each quarantine entry needs a nonempty statement and optional text reason')
        entries.append({'statement': item['statement'], 'reason': item.get('reason', '')})
    return entries


def load_packet(path):
    packet = read_regular(path, MAX_PACKET_BYTES)
    control.require(isinstance(packet, dict) and
                    {'summary', 'next_steps', 'completed', 'working_notes'} <= set(packet) <=
                    {'summary', 'next_steps', 'completed', 'working_notes', 'quarantine'},
                    'Checkpoint needs exactly summary, next_steps, completed, working_notes and optional quarantine')
    control.require(all(isinstance(packet[key], str) for key in ('summary', 'working_notes')),
                    'Checkpoint summary and working_notes must be text')
    for key in ('next_steps', 'completed'):
        control.require(isinstance(packet[key], list) and all(control.nonempty(item) for item in packet[key]),
                        f'Checkpoint {key} must be a list of nonempty text')
    if 'quarantine' in packet:
        packet['quarantine'] = quarantine_entries(packet['quarantine'])
    return packet


def atomic_text(path, content):
    fd, name = tempfile.mkstemp(prefix='.state-checkpoint-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
        # Durable state is already fsynced. These derived mirrors only need
        # atomic replacement; status can recreate them after any interruption.
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write(root, state):
    """Replace private mirrors after state commit; publish the manifest last."""
    root = Path(root)
    files = render(state)
    for name in NAMES:
        atomic_text(root / name, files[name])
    saved = manifest(state, files)
    atomic_text(root / MANIFEST, json.dumps(saved, indent=2, ensure_ascii=False) + '\n')
    return saved


def bundle(root, state):
    """Read-only restoration from the current ledger, never from the mirrors."""
    current = read_regular(Path(root) / 'state.json')
    control.require(identity(state) == identity(current), 'Stale or different-run checkpoint state; reload state.json')
    control.require(not current.get('pending'), 'Cannot restore context during an in-flight consultation')
    validate_checkpoint(current)
    files = render(current, strict=True)
    saved = manifest(current, files)
    return {**identity(current), 'files': files, 'digest': saved['digest'], 'checksums': saved['files']}

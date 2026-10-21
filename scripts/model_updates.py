"""Discover frontier models from public metadata without changing CLI configuration.

The returned profile describes requested models, never attests actual model use.
Callers retain explicit user pins and decide when to apply it. Refreshing a profile
does not change an already-running interactive session. Offline calls never make
requests or write the cache. ``checked_at`` is the last refresh attempt's UTC epoch
time; each source record separately dates the evidence for its selection.
"""
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import queue
import re
import tempfile
import threading
import time
import urllib.request

from providers import MODELS


SOURCES = {
    'claude': 'https://code.claude.com/docs/en/model-config.md',
    'codex': 'https://developers.openai.com/codex/models/',
    'kimi': 'https://moonshotai.github.io/kimi-code/en/configuration/config-files.html',
}
FETCH_TIMEOUT = 5.0
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,149}\Z')
GPT_ID = re.compile(r'\bgpt-\d+(?:\.\d+)?(?:-[a-z][a-z0-9-]*)?\b')
STRONGEST = re.compile(r'most\s+(?:capable|intelligent|powerful)|highest\s+intelligence|'
                       r'frontier\s+intelligence|strongest\s+(?:model|capability)', re.I)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        elif tag in ('p', 'div', 'section', 'h1', 'h2', 'h3', 'pre', 'li', 'tr', 'br'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ('p', 'div', 'section', 'h1', 'h2', 'h3', 'pre', 'li', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _plain(document):
    parser = _Text()
    parser.feed(document)
    return html.unescape(''.join(parser.parts))


def _http_fetch(url, timeout):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'ccloop-model-metadata/1.0',
        'Accept': 'text/markdown, text/html;q=0.9, text/plain;q=0.8',
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError('metadata exceeds size bound')
    return data.decode('utf-8')


# Kept as a narrow injection seam for deterministic metadata tests.
_fetch = _http_fetch


def _documents():
    """Parallel daemon workers bound caller wait even if DNS or a read stalls."""
    results = queue.Queue()
    deadline = time.monotonic() + min(FETCH_TIMEOUT, 15.0)

    def worker(provider, url):
        try:
            results.put((provider, _fetch(url, FETCH_TIMEOUT), None))
        except Exception as exc:
            # Exceptions may contain proxy credentials or local configuration.
            results.put((provider, None, type(exc).__name__))

    pending = set(SOURCES)
    for provider, url in SOURCES.items():
        threading.Thread(target=worker, args=(provider, url), daemon=True).start()
    documents, warnings = {}, []
    while pending:
        try:
            provider, document, error = results.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            break
        pending.discard(provider)
        if error:
            warnings.append(f'{provider}: official metadata unavailable ({error}); retained known selection.')
        else:
            documents[provider] = document
    for provider in sorted(pending):
        warnings.append(f'{provider}: official metadata timeout; retained known selection.')
    return documents, warnings


def _version(value):
    return tuple(int(part) for part in re.findall(r'\d+', value))


def _identity(provider, model, backend=None):
    if provider == 'claude':
        match = re.fullmatch(r'claude-(fable|opus|sonnet|haiku)-(\d+(?:-\d+)*)(?:\[1m\])?', model)
        if match:
            parts = _version(match[2])
            if parts[-1] > 10000000:  # A dated snapshot is not a newer generation.
                parts = parts[:-1]
            return match[1], parts
    elif provider == 'codex':
        match = re.fullmatch(r'gpt-(\d+(?:\.\d+)?)(?:-([a-z][a-z0-9-]*))?', model)
        if match:
            return match[2] or '', _version(match[1])
    else:
        match = re.fullmatch(r'(?:kimi-code/)?k(\d+(?:[.-]\d+)?)', backend or model)
        if match:
            return 'k', _version(match[1])
    return None


def _upgrade(provider, candidate, existing, backends=None):
    if candidate == existing:
        return True
    backends = backends or {}
    new = _identity(provider, candidate, backends.get(candidate))
    old = _identity(provider, existing, backends.get(existing))
    if new is None or old is None:
        return False  # Preserve unknown/custom selections instead of guessing.
    if new[0] == old[0]:
        return new[1] >= old[1]
    if provider == 'claude':
        tiers = {'fable': 3, 'opus': 2, 'sonnet': 1, 'haiku': 0}
        return tiers[new[0]] > tiers[old[0]]
    if provider == 'codex':
        tiers = {'astra': 4, 'sol': 3, 'terra': 2, 'luna': 1, 'mini': 0, 'spark': 0}
        if new[0] in tiers and old[0] in tiers:
            return tiers[new[0]] > tiers[old[0]] and new[1] >= old[1]
        return new[1] > old[1]
    return False


def _claude(document):
    candidates = re.findall(r'\bclaude-(?:fable|opus)-\d+(?:-\d+)*\b', document)
    if not candidates:
        return None
    return max(candidates, key=lambda model: (
        _identity('claude', model)[0] == 'fable', _identity('claude', model)[1]))


def _codex(document):
    """Associate an explicit capability statement with its closest exact ID."""
    text = _plain(document)
    models = list(GPT_ID.finditer(text))
    declared = []
    for statement in STRONGEST.finditer(text):
        nearby = [(min(abs(model.start() - statement.end()),
                       abs(statement.start() - model.end())), model.group())
                  for model in models]
        if nearby:
            distance, model = min(nearby)
            if distance <= 240:
                declared.append(model)
    if not declared:
        return None
    return max(declared, key=lambda model: _identity('codex', model)[1])


def _local_codex(warnings):
    path = Path.home() / '.codex' / 'models_cache.json'
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        candidates = []
        for entry in data['models']:
            model = entry.get('slug', '')
            if entry.get('visibility') != 'list' or not GPT_ID.fullmatch(model):
                continue
            if not STRONGEST.search(entry.get('description', '')):
                continue
            candidates.append((entry.get('priority', 999), model))
        return min(candidates)[1] if candidates else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        warnings.append('codex: local models_cache.json metadata format is unreadable; retained known selection.')
        return None


def _local_kimi(warnings):
    path = Path.home() / '.kimi' / 'config.toml'
    if not path.exists():
        return {}
    try:
        import tomllib
    except ImportError:
        warnings.append('kimi: Python 3.11+ is needed to inspect configured aliases; retained known selection.')
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
        aliases = {}
        for alias, value in data.get('models', {}).items():
            backend = value.get('model', '')
            if (MODEL_ID.fullmatch(alias) and _identity('kimi', alias, backend)
                    and value.get('provider') in data.get('providers', {})):
                aliases[alias] = backend
        return aliases
    except (OSError, ValueError, TypeError, AttributeError):
        warnings.append('kimi: configured alias metadata format is unreadable; retained known selection.')
        return {}


def _read_cache(path, warnings):
    if not path.exists():
        return {}
    try:
        if path.is_symlink():
            raise ValueError('symlink cache')
        data = json.loads(path.read_text(encoding='utf-8'))
        checked = data['checked_at']
        if not isinstance(checked, (float, int)) or not math.isfinite(checked):
            raise ValueError('invalid timestamp')
        models = data['models']
        if not all(isinstance(models.get(p), str) and MODEL_ID.fullmatch(models[p]) for p in MODELS):
            raise ValueError('invalid models')
        sources = data.get('sources', {})
        if (not isinstance(sources, dict)
                or not all(isinstance(sources.get(p, []), list) for p in MODELS)
                or not isinstance(data.get('warnings', []), list)):
            raise ValueError('invalid sources')
        for records in sources.values():
            for record in records:
                if (not isinstance(record, dict) or not isinstance(record.get('model'), str)
                        or not MODEL_ID.fullmatch(record['model'])
                        or ('backend' in record and not isinstance(record['backend'], str))):
                    raise ValueError('invalid source record')
        return {'models': {p: models[p] for p in MODELS}, 'checked_at': checked,
                'sources': {p: sources.get(p, []) for p in MODELS},
                'warnings': [w for w in data.get('warnings', []) if isinstance(w, str)]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        warnings.append('Model metadata cache is unreadable; retained current profile.')
        return {}


def _write_cache(path, data):
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('cache path must not be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.models-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def resolve_models(current=None, *, force=False, offline=False, cache_path=None, ttl_seconds=1800):
    """Return a complete requested-model profile, retaining stronger known choices.

    ``CCLOOP_MODEL_CACHE`` can relocate the private cache, including in tests.
    Public documents contain selection evidence, not account-access proof. Source
    outages or changed formats produce warnings and never authorize a downgrade.
    """
    offline = offline or os.environ.get('CCLOOP_MODEL_METADATA_OFFLINE', '').lower() in ('1', 'true', 'yes')
    models = dict(MODELS)
    for provider, model in (current or {}).items():
        if provider in MODELS and isinstance(model, str) and MODEL_ID.fullmatch(model):
            models[provider] = model
    warnings = []
    path = Path(cache_path or os.environ.get('CCLOOP_MODEL_CACHE') or
                Path.home() / '.cache' / 'ccloop' / 'models.json').expanduser()
    cached = _read_cache(path, warnings)
    sources = {p: [] for p in MODELS}
    backends = {}
    for records in cached.get('sources', {}).values():
        for record in records:
            if isinstance(record, dict) and 'backend' in record:
                backends[record.get('model')] = record['backend']
    for provider, model in cached.get('models', {}).items():
        if _upgrade(provider, model, models[provider], backends):
            models[provider] = model
            sources[provider] = cached['sources'][provider]
    now = time.time()
    checked = cached.get('checked_at', 0.0)
    fresh = bool(cached) and 0 <= now - checked < ttl_seconds
    if offline or (fresh and not force):
        return {'models': models, 'checked_at': checked, 'sources': sources,
                'warnings': warnings + cached.get('warnings', []), 'refreshed': False}

    documents, failures = _documents()
    warnings.extend(failures)
    configured = _local_kimi(warnings)
    backends.update(configured)

    def accept(provider, candidate, source, evidence, backend=None):
        if not candidate:
            return
        if not _upgrade(provider, candidate, models[provider], backends):
            warnings.append(f'{provider}: ignored older or incomparable metadata to avoid a model downgrade.')
            return
        models[provider] = candidate
        record = {'source': source, 'model': candidate, 'checked_at': now, 'evidence': evidence}
        if backend:
            record['backend'] = backend
        sources[provider] = [record]

    for provider, document in documents.items():
        candidate = None
        if provider == 'claude':
            candidate = _claude(document)
            accept(provider, candidate, SOURCES[provider], 'Highest documented Fable version, otherwise Opus.')
        elif provider == 'codex':
            candidate = _codex(document)
            accept(provider, candidate, SOURCES[provider], 'Exact ID beside an official strongest-capability statement.')
        else:
            match = re.search(r'default_model\s*=\s*[\"\'](kimi-code/k\d+(?:[.-]\d+)?)[\"\']', _plain(document))
            candidate = match[1] if match else None
            if candidate and configured:
                target = _identity('kimi', candidate)[1]
                matches = [alias for alias, backend in configured.items()
                           if _identity('kimi', alias, backend)[1] == target]
                if matches:
                    selected = candidate if candidate in matches else sorted(matches)[0]
                    accept('kimi', selected, SOURCES['kimi'], 'Official default backend matches a configured CLI alias.',
                           configured[selected])
                else:
                    warnings.append('kimi: official frontier model has no configured alias; retained strongest known configured selection.')
                    selected = max(configured, key=lambda alias: _identity('kimi', alias, configured[alias])[1])
                    accept('kimi', selected, '~/.kimi/config.toml',
                           'Newest configured K-series backend; official next model is not configured.',
                           configured[selected])
            elif candidate:
                warnings.append('kimi: official frontier alias is not verified in local configuration; retained known selection.')
        if not candidate:
            warnings.append(f'{provider}: official metadata format has no supported frontier selection; retained known profile.')

    local = _local_codex(warnings)
    if local:
        accept('codex', local, '~/.codex/models_cache.json',
               'Highest-priority visible model explicitly described as frontier intelligence.')
    result = {'models': models, 'checked_at': now, 'sources': sources,
              'warnings': warnings, 'refreshed': True}
    try:
        _write_cache(path, result)
    except (OSError, ValueError):
        warnings.append('Could not write private model metadata cache; returned selections remain usable.')
    return result

"""Find user-installed native clients when a noninteractive shell omits their bins."""
import os
from pathlib import Path
import re
import shutil


def client(provider):
    """Resolve the actual client, preferring Code and scheduler-free loop profiles."""
    override = os.environ.get('CCLOOP_' + provider.upper() + '_BIN')
    if override is not None:
        candidate = override
    elif provider == 'claude':
        direct = Path.home() / '.local/share/ccloop/launchers/claude-direct'
        candidate = str(direct) if direct.is_file() and os.access(direct, os.X_OK) else provider
    elif provider == 'kimi':
        import kimi_runtime
        home = Path.home()
        candidates = [home / '.local/share/ccloop/launchers/kimi-code-direct',
                      home / '.local/bin/kimi-code',
                      home / '.local/share/ccloop/clients/kimi-code/node_modules/.bin/kimi',
                      home / '.kimi-code/bin/kimi']
        for name in ('kimi-code', 'kimi'):
            found = shutil.which(name)
            if found and kimi_runtime.kind(found) == 'code':
                candidates.append(Path(found))
        for path in candidates:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.absolute()), str(path)
        return None, 'kimi-code'
    else:
        candidate = provider
    found = shutil.which(os.path.expanduser(candidate))
    return (str(Path(found).absolute()) if found else None), candidate


def configure():
    """Extend this process's PATH only; retain existing choices and explicit overrides."""
    home = Path.home()
    nvm = Path(os.environ.get('NVM_DIR', home / '.nvm')).expanduser()
    try:
        versions = [path for path in (nvm / 'versions' / 'node').iterdir()
                    if re.fullmatch(r'v\d+(?:\.\d+)*', path.name) and path.is_dir()]
    except OSError:
        versions = []
    versions.sort(key=lambda path: tuple(int(part) for part in path.name[1:].split('.')), reverse=True)
    candidates = {
        'codex': [home / '.local/bin', nvm / 'current/bin',
                  *(path / 'bin' for path in versions), home / '.npm-global/bin'],
        'claude': [home / '.local/bin', home / '.npm-global/bin'],
        'kimi': [home / '.local/bin', home / '.kimi-code/bin'],
    }
    paths = os.environ.get('PATH', '').split(os.pathsep)
    for provider, directories in candidates.items():
        if os.environ.get('CCLOOP_' + provider.upper() + '_BIN') or shutil.which(provider, path=os.pathsep.join(paths)):
            continue
        for directory in directories:
            executable = directory / provider
            if executable.is_file() and os.access(executable, os.X_OK):
                if str(directory) not in paths:
                    paths.append(str(directory))
                break
    os.environ['PATH'] = os.pathsep.join(paths)


def client_env(executable, environment=None):
    """Use an NVM client's paired Node only in that child, preserving the caller."""
    result = dict(os.environ if environment is None else environment)
    path = Path(executable).expanduser()
    if not path.is_absolute():
        found = shutil.which(str(path), path=result.get('PATH'))
        if found is None:
            return result
        path = Path(found)
    try:
        resolved = path.resolve()
        with resolved.open('rb') as source:
            header = source.readline(256)
        if not re.match(rb'^#![^\n]*\bnode(?:\s|$)', header):
            return result
        candidates = [path.parent / 'node']
        candidates.extend(parent / 'bin/node' for parent in resolved.parents
                          if parent.parent.name == 'node' and parent.parent.parent.name == 'versions'
                          and re.fullmatch(r'v\d+(?:\.\d+)*', parent.name))
        for node in candidates:
            if node.is_file() and os.access(node, os.X_OK):
                directory = str(node.parent)
                paths = [entry for entry in result.get('PATH', '').split(os.pathsep) if entry != directory]
                result['PATH'] = os.pathsep.join([directory, *paths])
                break
    except (OSError, RuntimeError):
        # The native caller retains its normal executable/error classification.
        pass
    return result

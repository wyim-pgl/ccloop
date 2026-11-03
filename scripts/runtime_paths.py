"""Find user-installed native clients when a noninteractive shell omits their bins."""
import os
from pathlib import Path
import re
import shutil


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
        'kimi': [home / '.local/kimi-shim', home / '.local/bin',
                 home / '.local/share/uv/tools/kimi-cli/bin'],
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

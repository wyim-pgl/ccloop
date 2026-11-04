#!/usr/bin/env python3
"""Run a native client directly in an existing image, without a scheduler or shell."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys


SCHEDULERS = {'sbatch', 'srun', 'salloc'}
ENVIRONMENT = {'PATH', 'BASH_ENV', 'HOME', 'LANG', 'LC_ALL', 'TERM', 'LD_LIBRARY_PATH'}


def command(profile, arguments):
    if not isinstance(profile, dict) or set(profile) != {'singularity', 'image', 'executable', 'binds', 'environment'}:
        raise ValueError('Direct profile needs singularity, image, executable, binds and environment')
    for key in ('singularity', 'executable'):
        value = profile[key]
        if not isinstance(value, str) or '\0' in value or not Path(value).is_absolute():
            raise ValueError('Direct executable paths must be absolute')
        if Path(value).name in SCHEDULERS:
            raise ValueError('A direct client cannot use a scheduler executable')
    singularity = shutil.which(profile['singularity'])
    if not singularity or not Path(profile['image']).is_file():
        raise ValueError('Direct container runtime or image is unavailable')
    binds, settings = profile['binds'], profile['environment']
    if not isinstance(binds, list) or not all(isinstance(value, str) and value.startswith('/')
                                             and '\0' not in value for value in binds):
        raise ValueError('Container binds must be absolute path specifications')
    if not isinstance(settings, dict) or not set(settings) <= ENVIRONMENT:
        raise ValueError('Profile environment permits only non-credential transport settings')
    if not all(isinstance(value, str) and '\0' not in value for value in settings.values()):
        raise ValueError('Profile environment settings must be text')
    current = str(Path.cwd())
    home = str(Path.home())
    extra = [home + ':' + home, current + ':' + current]
    environment = dict(os.environ)
    code_home = environment.get('KIMI_CODE_HOME')
    if code_home:
        path = Path(code_home).expanduser().absolute()
        if not path.is_dir() or any(char in str(path) for char in (':', ',', '\0')):
            raise ValueError('KIMI_CODE_HOME must be an existing bindable directory')
        extra.append(str(path) + ':' + str(path))
        settings = dict(settings, KIMI_CODE_HOME=str(path))
    for name in ('CCLOOP_REVIEWER', 'CCLOOP_CONSULTANT', 'CCLOOP_MANAGED_WORKER'):
        if name in environment:
            settings = dict(settings, **{name: environment[name]})
    argv = [singularity, 'exec']
    for value in dict.fromkeys([*binds, *extra]):
        argv += ['--bind', value]
    argv += ['--pwd', current]
    for key, value in settings.items():
        argv += ['--env', key + '=' + value]
    argv += [profile['image'], profile['executable'], *arguments]
    return argv, environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, type=Path)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    try:
        profile = json.loads(args.profile.read_text(encoding='utf-8'))
        invocation, environment = command(profile, arguments)
        os.execve(invocation[0], invocation, environment)
    except (ValueError, TypeError, OSError, KeyError) as error:
        print('ccloop direct container: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

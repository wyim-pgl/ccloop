#!/usr/bin/env python3
"""Start an interactive ccloop driver with explicit frontier model settings.

A bounded, tool-free readiness request checks startup access before the child
inherits the terminal unchanged. Access failures can hand off to Kimi once.
Malformed or unsafe probes stop. Mid-session failures retain their exit status
and explain how to request a handoff; the terminal is never piped or intercepted.
"""

import argparse
import errno
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import ccloop
import providers
from providers import MODELS


SKILL = Path(__file__).resolve().parents[1] / 'SKILL.md'
READINESS_PROMPT = (
    'This is a tool-free model access readiness check. Do not execute a task, '
    'read files, call tools, or invoke any agent or CLI. Return only the JSON '
    'object {"status":"ready"}.'
)


def executable(driver):
    """Resolve one executable path, never a shell command or argument string."""
    candidate = os.environ.get('CCLOOP_' + driver.upper() + '_BIN', driver)
    found = shutil.which(os.path.expanduser(candidate))
    return str(Path(found).absolute()) if found else None, candidate


def prompt_for(driver, goal, models, root, fallback=False, auto_update_models=True):
    if driver == 'claude':
        start = '/ccloop'
    elif driver == 'codex':
        start = '$ccloop'
    else:
        start = f'Read and follow the ccloop skill at {json.dumps(str(SKILL))}.'
    instructions = [
        start,
        f'Act as the interactive {driver} driver and carry out the goal below. '
        'Obtain independent reviews at the plan, milestone, and final gates.',
        'Continue autonomously until the original goal is verified complete, the user '
        'explicitly stops, an explicit user budget is exhausted, or required authorization '
        'or credentials cannot be obtained. Do not end the turn after one milestone or '
        'ask the user "continue?". Send ordinary technical and preference questions '
        'immediately to the other model through the ccloop helper ask command. Record '
        'the peer answer and your choice, then act within the existing user authorization. '
        'Maintain the private run ccloop.md journal: use the helper record command '
        'with an explicit --after-round and --entry JSON for the choice before work, '
        'then for executed actions, actual checks and the final user-facing conclusion. '
        'Return its journal_path when finished. '
        'Reviewer advice does not grant user authorization, supply missing credentials, '
        'or bypass safety and permission controls.',
        'Use these run-scoped driver/reviewer model selections: ' + json.dumps(models) + '.',
        'Use max reasoning effort for Claude and Codex, and thinking for Kimi. '
        'Record the requested and observed models separately; do not silently '
        'substitute a weaker model.',
    ]
    if root:
        instructions.append(
            f'Resume the existing ccloop ledger at {json.dumps(root)}. '
            'Read its status and saved checkpoint first. Preserve its original '
            'goal, criteria, budgets, and review/claim/issue history. Do not '
            'initialize a replacement run or reset budgets. If the driver changed, '
            'perform the documented driver handoff before continuing. An explicit '
            'user stop still requires user authorization to resume.'
        )
    else:
        instructions.append(
            'Create a private ccloop ledger outside the project and record its '
            'path and model selections before substantial work. Honor explicit user '
            'limits; otherwise leave max_rounds, max_hours, and max_no_progress_reviews '
            'unlimited (null). Do not invent time, round, or no-progress caps. Initialize its '
            'model settings with ' + json.dumps({'models': models,
                                                'auto_update_models': auto_update_models}) + '. '
            + ('Keep automatic model updates enabled for this default profile.'
               if auto_update_models else
               'An explicit CLI model override pins this profile; keep automatic model updates disabled.')
        )
    if driver == 'kimi':
        instructions.append(
            'This is a Kimi driver handoff.' if fallback else 'Kimi is the driver.'
        )
        instructions.append(
            'Use a reachable Claude or Codex reviewer; never use Kimi to review itself.'
        )
    return '\n\n'.join(instructions) + '\n\nGoal (verbatim):\n' + goal


def command_for(driver, command, goal, models, root, fallback=False, auto_update_models=True):
    prompt = prompt_for(driver, goal, models, root, fallback, auto_update_models)
    if driver == 'claude':
        return [command, '--model', models[driver], '--effort', 'max', prompt]
    if driver == 'codex':
        return [command, '--model', models[driver], '-c',
                'model_reasoning_effort="max"', prompt]
    return [command, '--model', models[driver], '--thinking', '--prompt', prompt]


def launch_plan(args, models, root):
    driver = args.driver
    command, candidate = executable(driver)
    reason = None
    if command is None:
        reason = f'{driver} executable is missing or not executable: {candidate}'
        if driver != 'kimi':
            driver = 'kimi'
            command, candidate = executable(driver)
            if command is None:
                reason += f'; kimi executable is missing or not executable: {candidate}'
    fallback = driver != args.driver
    return {
        'requested_driver': args.driver,
        'driver': driver,
        'argv': command_for(driver, command or candidate, args.goal, models, root, fallback,
                            not any(getattr(args, name + '_model') is not None for name in MODELS)),
        'project': str(args.project),
        'root': root,
        'models': models,
        'fallback': fallback,
        'available': command is not None,
        'reason': reason,
    }


def run_driver(command, project):
    """Preserve normal terminal behavior, including child output and input."""
    try:
        code = subprocess.run(command, cwd=project).returncode
    except OSError as exc:
        # An executable can disappear after preflight or have a missing interpreter.
        if exc.errno == errno.ENOENT:
            return 127, str(exc)
        if exc.errno in (errno.EACCES, errno.ENOEXEC):
            return 126, str(exc)
        return 1, str(exc)
    return (128 - code if code < 0 else code), None


def handoff_command(args, root):
    command = [sys.executable, str(Path(__file__).resolve()), '--driver', 'kimi',
               '--project', str(args.project), '--goal', args.goal]
    if root:
        command += ['--root', root]
    return shlex.join(command)


def ledger_handoff_command(root, driver):
    return shlex.join([sys.executable, str(SKILL.parent / 'scripts' / 'ccloop.py'),
                      '--root', root, 'handoff', '--driver', driver,
                      '--reason', 'Explicit driver access handoff'])


def read_run(args, root, overrides, driver, expected=None):
    """Refuse work that would alter or bypass an existing run's frozen contract."""
    state = ccloop.read(Path(root))
    config = state['config']
    ccloop.require(config['goal'] == args.goal, 'Goal differs from the frozen ledger goal')
    ccloop.require(Path(config['project']).resolve() == args.project,
                   'Project differs from the frozen ledger project')
    models = state.get('active_models', config['models'])
    ccloop.require(isinstance(models, dict) and set(models) == set(MODELS)
                   and all(isinstance(value, str) and value.strip() for value in models.values()),
                   'Saved ledger model selections are invalid')
    ccloop.require(all(value == models[name] for name, value in overrides.items()),
                   'Model override differs from the frozen ledger model selections')
    ccloop.require(state.get('phase') not in ('stopped', 'complete'),
                   'Ledger is stopped or complete; the launcher cannot resume it')
    ccloop.require(not state.get('pending'), 'Ledger consultation is in flight')
    exhausted = ccloop.limit(state)
    ccloop.require(not exhausted, 'Ledger budget exhausted: ' + str(exhausted))
    if not state.get('enabled') or state.get('phase') != 'ready':
        message = 'Ledger is blocked; explicitly reconcile it before launching'
        if state.get('failure_kind') == 'access':
            message += '. First run the helper handoff: ' + ledger_handoff_command(root, driver)
        raise ValueError(message)
    ccloop.require(state.get('active_driver') == driver,
                   'Ledger has another active driver. First run the helper handoff: '
                   + ledger_handoff_command(root, driver))
    if expected is not None:
        ccloop.require(state['generation'] == expected['generation']
                       and state['run_id'] == expected['run_id']
                       and models == expected.get('active_models', expected['config']['models']),
                       'Ledger changed during launch; inspect the saved checkpoint first')
    return state, dict(models)


def readiness(driver, models, timeout, cancelled=None):
    cancelled = cancelled or (lambda: False)
    config = {'models': models, 'commands': {}}
    for name in MODELS:
        found, candidate = executable(name)
        config['commands'][name] = [found or candidate]
    with tempfile.TemporaryDirectory(prefix='ccloop-access-') as directory:
        attempt, report = providers.invoke(
            driver, config, READINESS_PROMPT, Path(directory) / driver, timeout,
            cancelled=cancelled, started=lambda details: not cancelled(),
        )
    if attempt['status'] == 'received' and report != {'status': 'ready'}:
        attempt.update(status='invalid', failure_kind='invalid',
                       error='Readiness probe did not return exactly {"status":"ready"}')
    evidence = {name: attempt[name] for name in
                ('provider', 'requested_model', 'observed_models', 'status', 'failure_kind', 'error')
                if name in attempt}
    print('ccloop readiness: ' + json.dumps(evidence, ensure_ascii=False), file=sys.stderr)
    return attempt


def fallback_to_kimi(args, root, overrides, state, driver, reason):
    command, candidate = executable('kimi')
    if command is None:
        raise FileNotFoundError(f'kimi executable is missing or not executable: {candidate}')
    if root:
        state, _ = read_run(args, root, overrides, driver, expected=state)
        state = ccloop.change_state(Path(root), 'handoff', reason, driver='kimi',
                                    expected_generation=state['generation'])
    print(f'ccloop: {reason}; starting kimi fallback.', file=sys.stderr)
    return state


def interactive(args, models, root, overrides, state, plan):
    driver = plan['driver']
    fallback = plan['fallback']
    if fallback:
        state = fallback_to_kimi(args, root, overrides, state, args.driver, plan['reason'])
    while True:
        if root:
            state, _ = read_run(args, root, overrides, driver, expected=state)
        timeout = args.access_timeout
        if state is not None and state['deadline'] is not None:
            timeout = min(timeout, max(0.001, state['deadline'] - time.time()))

        def cancelled():
            if root is None:
                return False
            try:
                read_run(args, root, overrides, driver, expected=state)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                return True
            return False

        attempt = readiness(driver, models, timeout, cancelled=cancelled)
        if attempt['status'] != 'received':
            if attempt.get('failure_kind') == 'access' and driver != 'kimi':
                reason = f'{driver} readiness failed: {attempt.get("error", "access unavailable")}'
                state = fallback_to_kimi(args, root, overrides, state, driver, reason)
                driver, fallback = 'kimi', True
                continue
            return 130 if attempt.get('failure_kind') == 'interrupted' else 2
        if root:
            state, _ = read_run(args, root, overrides, driver, expected=state)
        command, candidate = executable(driver)
        code, error = run_driver(
            command_for(driver, command or candidate, args.goal, models, root, fallback,
                        auto_update_models=not overrides), args.project,
        )
        if error:
            print(f'ccloop: {driver}: {error}', file=sys.stderr)
        if code in (126, 127) and driver != 'kimi' and root:
            state = fallback_to_kimi(args, root, overrides, state, driver,
                                     f'{driver} exited with status {code}')
            driver, fallback = 'kimi', True
            continue
        if code and code != 130:
            print(f'ccloop: {driver} session exited with status {code}.', file=sys.stderr)
            if driver != 'kimi':
                print('ccloop: Mid-session authentication/quota/model-access failures cannot '
                      'be distinguished reliably from other failures without intercepting '
                      'the terminal. For an explicit Kimi handoff, run:', file=sys.stderr)
                if root:
                    print(ledger_handoff_command(root, 'kimi'), file=sys.stderr)
                print(handoff_command(args, root), file=sys.stderr)
                if root is None:
                    print('Include --root with the saved private ledger path to preserve '
                          'the checkpoint, original criteria, and remaining budgets. '
                          'The launcher will not create a replacement run after an '
                          'interactive session has started.', file=sys.stderr)
        return code


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog='CCLOOP_CLAUDE_BIN, CCLOOP_CODEX_BIN, and CCLOOP_KIMI_BIN may each '
               'name one executable path; they are never interpreted as shell commands.',
    )
    parser.add_argument('goal_words', nargs='*', metavar='GOAL',
                        help='Goal text; quote it to preserve whitespace verbatim')
    parser.add_argument('--driver', default='codex', choices=tuple(MODELS),
                        help='Interactive driver (default: codex)')
    parser.add_argument('--project', default=Path.cwd(), type=Path,
                        help='Project directory (default: current directory)')
    parser.add_argument('--goal', help='Verbatim goal text instead of a positional goal')
    parser.add_argument('--root', type=Path, help='Existing private ledger to resume')
    for driver in MODELS:
        parser.add_argument('--' + driver + '-model')
    parser.add_argument('--access-timeout', type=float, default=300,
                        help='Positive finite readiness timeout in seconds (default: 300)')
    parser.add_argument('--offline-models', action='store_true',
                        help='Use cached model metadata without contacting model-list sources')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print a JSON launch plan without calling any CLI')
    args = parser.parse_args(argv)
    if args.goal is not None and args.goal_words:
        parser.error('provide either a positional goal or --goal, not both')
    if args.goal is None:
        args.goal = ' '.join(args.goal_words)
    if not args.goal.strip():
        parser.error('goal must not be empty')
    args.project = args.project.expanduser().resolve()
    if not args.project.is_dir():
        parser.error(f'project directory does not exist: {args.project}')
    overrides = {driver: getattr(args, driver + '_model') for driver in MODELS
                 if getattr(args, driver + '_model') is not None}
    if any(not model.strip() for model in overrides.values()):
        parser.error('model IDs must not be empty')
    if not math.isfinite(args.access_timeout) or args.access_timeout <= 0:
        parser.error('access-timeout must be positive and finite')
    root = str(args.root.expanduser().resolve()) if args.root else None
    state = None
    try:
        if root:
            state, models = read_run(args, root, overrides, args.driver)
            if not args.offline_models and not args.dry_run:
                state = ccloop.refresh_models(Path(root), for_consult=True)
                state, models = read_run(args, root, overrides, args.driver, expected=state)
        else:
            from model_updates import resolve_models
            resolved = resolve_models(current=MODELS, offline=args.offline_models or args.dry_run)
            models = dict(resolved['models'], **overrides)
            for warning in resolved.get('warnings', []):
                print('ccloop models: ' + warning, file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    plan = launch_plan(args, models, root)
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    if not plan['available']:
        print('ccloop: ' + plan['reason'], file=sys.stderr)
        return 127
    try:
        return interactive(args, models, root, overrides, state, plan)
    except FileNotFoundError as error:
        print('ccloop: ' + str(error), file=sys.stderr)
        return 127
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

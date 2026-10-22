# Context checkpoints and managed sessions

Use managed mode when the goal should continue across worker context replacement:

```bash
ccloop --managed 'Complete the original goal and verify its result'
ccloop --managed --root /private/existing-run --context-threshold 0.7 \
  --goal 'The exact existing goal'
```

The shorthand still defaults to Codex in the current directory. Select the actual
driver with `--driver claude`, `--driver codex` or a justified Kimi handoff. The
original interactive mode remains available without `--managed`. It injects the
same validated recovery content when starting from an existing `--root`.

## The recovery files

| File | Content |
| --- | --- |
| `intend.md` | Original goal, authorization, success criteria and explicit limits |
| `resume.md` | Current state, decisions, actual verification versus driver reports, issues and saved work notes |
| `todo.md` | Explicit next steps and original criteria/issues still requiring work |
| `quarantine.md` | Driver-reported superseded or refuted notes, refuted review claims, resolved issues and the previous driver checkpoint; never reuse these as facts |

These private files sit beside `state.json` and `ccloop.md`. The durable ledger
is authoritative. The helper generates bounded views from it and repairs missing
or edited views; arbitrary Markdown changes do not grant new authorization or
rewrite the goal. A run/revision/generation identity and content digests bind
the recovery bundle to its source. A frozen contract too large for the recovery
budget is explicitly refused, rather than silently truncated.

New managed runs exclude the generated Graft internals `graft/.cache/**` and
`graft/.graph/**` from artifact drift detection. Tool telemetry may update those
files after a worker exits. The frozen `watch_exclude` list makes this explicit;
existing runs keep their original scope. Material goal files remain watched.

## Save and restore actual work

After each bounded step, save a JSON packet such as:

```json
{
  "summary": "Implemented the parser; its regression check passed.",
  "next_steps": ["Inspect the next failing integration check."],
  "completed": ["Updated parser.py and ran the actual regression check."],
  "working_notes": "The selected format is compatible with the original examples."
}
```

An optional `quarantine` list carries entries that a fresh context must not
reuse: plain strings or `{"statement": ..., "reason": ...}` objects, at most 50
per packet. The ledger keeps the newest 200 distinct statements with the revision
that reported them.

Then record it without altering the existing run contract:

```bash
python3 /path/to/ccloop/scripts/ccloop.py --root /private/run checkpoint \
  --packet /private/run/checkpoint.json
python3 /path/to/ccloop/scripts/ccloop.py --root /private/run context
```

`context` returns the actual file text for restoration. The manager and existing-root
interactive launcher put this validated text into a new worker's initial prompt.
Paths establish provenance; the file contents supply the working context. Keep
specific commands/results in the existing work journal and evidence packets.
Older runs retain explicit gaps where driver work was never recorded.

## Clear on request

`clear --packet FILE --reason TEXT` is the explicit `/ccloop clear` flow for an
interactive driver. It saves the packet as a checkpoint, regenerates all four
files, and dispatches their actual text to the opposite model with
`--stage handoff`, which assesses whether a fresh context can continue faithfully
from them. The reviewer's issues name missing steps, unevidenced driver claims
and wrong or missing quarantine entries. An accepted review records a `clear`
event with the review hash, the bundle digest and the restore command, and the
result prints `restore` lines for Claude Code (`/clear` then `/ccloop resume ROOT`),
Codex (`/new` then `$ccloop resume ROOT`) and the terminal launcher. The handoff
review resets the 30-minute review clock like a milestone review. A rejected
review keeps the checkpoint and exits nonzero. The helper never clears the
host's chat; the user performs that step and resumes with the same root.

When a managed runtime owns the run (a live manager lock or pending managed
work), `clear` writes only a private `managed/rotate-requested.json` marker
instead of committing to the ledger, because a commit would supersede the
in-flight native turn. The manager consumes the marker at its next accepted turn,
starts a fresh worker context, and records `rotate_requested` with the reason in
the `managed_turn_accepted` event.

## Context replacement

The manager runs bounded work turns. It uses native current-thread context usage
when the client supplies it; the default threshold is **0.7** of the reported
window. Cumulative billing tokens are not a current-context measurement. Clients
without a reliable current-context signal use fresh sessions per bounded turn;
the manager identifies that policy instead of inventing a usage percentage.

At a safe turn boundary it saves the checkpoint, creates a fresh worker context,
and supplies the four-file bundle and accepted peer advice. It preserves the
same goal, run ID, original limits, claims/issues and 30-minute review timing.
Questions go through the opposite model; a worker's completion claim triggers
independent final review and cannot establish completion alone.

The manager records work-turn intent before starting native work. An interrupted
turn with uncertain side effects must be reconciled before more work; restarting
the manager is not permission to replay it. Explicit stops remain stopped.
Inspect the actual files, native output and running processes first. Save a packet
with the four checkpoint fields above plus `checks`, a nonempty list of actual
`method`, `result` and `evidence` observations. Then retire the exact interrupted
turn or blocker ID shown in `status`:

```bash
python3 /path/to/ccloop/scripts/ccloop.py --root /private/run reconcile-managed \
  --expected-turn THE_RECORDED_ID --reason 'Inspected the actual outcome' \
  --packet /private/run/reconciled.json
```

This records the inspected outcome and retires that pending action. It preserves
the disabled/stopped state; continuation still uses the existing authorized
`resume --reason ...` command. A live recorded worker cannot be retired, and an
explicit user stop requires the user's continuation before resuming.

Native permission denials remain permission outcomes. Authentication, model or
service access failures before work may use the existing independent Kimi fallback
policy. Access errors after work starts require outcome reconciliation.

The manager must remain running for automatic continuation. This feature starts
worker sessions under that process; it does not clear the current app's chat or
install an OS daemon. Runtime prompts, native outputs and recovery files stay
in the private supervisor directory when the skill source is published.

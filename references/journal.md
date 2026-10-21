# The ccloop.md journal

Every run automatically maintains `RUN_ROOT/ccloop.md` beside `state.json` in the
private supervisor directory. Choose a persistent run directory, normally under
`$XDG_STATE_HOME/ccloop/` or `~/.local/state/ccloop/`, outside the project and skill.
`status` returns its absolute `journal_path`.
The helper records the exact outgoing request and supplied context, originating
driver, each attempted peer/provider, requested model, observed model when the
CLI actually reports it, returned answers/reviews, failed attempts, Kimi fallback,
rejected responses and stop/resume/handoff events. An unknown observed model
stays `unverified`; the driver's configured model is not an observation.

Read the journal as a timeline: **request → model attempt → accepted answer →
driver choice → executed work and checks → final review and conclusion**.
Each entry has a UTC timestamp, revision, round and phase. Request and response
text is fenced so embedded Markdown remains quoted evidence. Raw rejected output
is marked unaccepted; acceptance is explicit. This is an external conversation
and work record, not a record of a model's private reasoning.

## Record the driver's part

The helper cannot observe work performed by an interactive host. Record a choice
before acting, with empty actions/checks, then append the actions actually
performed and observed checks after each bounded step. Include the user-facing
final conclusion in the last entry's summary. Preserve failed checks as well as
successful ones. If work is interrupted before an outcome was recorded, reconcile
the actual artifacts before reporting what happened.

Use [the work entry example](../assets/ccloop.work.example.json) as an input file:

```bash
python3 /path/to/ccloop/scripts/ccloop.py --root /private/run record \
  --after-round 2 --entry /private/run/work.json
```

`--after-round` explicitly names the accepted question or review that prompted
the work. Use its returned `round`, even if newer questions have since happened.
Use `0` for a checkpoint with no associated peer consultation. The helper stores
that originating attempt and artifact hash separately from the current artifact
hash, so it can distinguish advice inputs from work outputs.

The entry contains exactly `summary`, `decision`, `actions` and `checks`.
Summary and decision are nonempty text; actions is a list of actual actions;
checks is a list of `method`, `result` and `evidence` text objects. Empty lists
are valid for a decision recorded before any action/check. Work entries are
**driver-reported** and cannot establish criteria, resolve reviewer issues,
change consultation budgets or complete the goal. Independent final review is
still required. After final review, `record` remains available to preserve the
actual outcome and final conclusion before returning to the user.

## Recovery and privacy

The Markdown is atomically regenerated under the same lock as the ledger. The
durable source is the committed history plus saved prompt/response artifacts.
`status` or `journal` rebuilds it after an interrupted write or for an existing
run without a journal, without changing the original ledger or replaying work:

```bash
python3 /path/to/ccloop/scripts/ccloop.py --root /private/run journal
```

Missing, changed or unreadable prompt artifacts are flagged. Older runs retain
their original evidence gaps; the journal cannot recover unrecorded driver work
or invent model observations. All runtime files remain in the private run
directory. Give the user the journal path. Publishing the skill's source does
not authorize publishing these conversations; export/share only when requested.

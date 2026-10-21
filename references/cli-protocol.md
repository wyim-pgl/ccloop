# CLI cross-review protocol

Use Python 3.10+ and resolve the installed skill's actual directory. Examples below
abbreviate the absolute helper path as `scripts/ccloop.py`; they run from the repo.
Run state belongs in a private supervisor directory outside the working project.

```bash
python3 scripts/ccloop.py doctor
python3 scripts/ccloop.py --root /work/private-supervisor init --config /work/run.json
python3 scripts/ccloop.py --root /work/private-supervisor status
python3 scripts/ccloop.py --root /work/private-supervisor consult \
  --packet /work/private-supervisor/plan.json --stage plan
```

Create `run.json` from [the template](../assets/ccloop.config.example.json).
The original configuration, goal and criteria are frozen in the ledger. The
supervisor retains the original layout: `CONTROL.md`, `state.json`, `reviews/`,
`prompts/` and `observations/`. Defaults
are 12 consultation rounds, two hours, three consecutive milestone reviews without
verified progress, a 300-second per-call timeout, and a **30-minute review interval**.
The review interval is separate from the whole-run time limit and call timeout.
Status exposes `last_review_at`, `next_review_at` and `review_due`; the active host
checks these while working. Plan and final reviews happen immediately. The
`heartbeat` command prints a durable wakeup prompt but creates no schedule.
Choose task-appropriate
limits from the user's instruction. Changing a config file does not reset a run.
The `watch` list defines drift detection coverage; include material file formats.
Automatic model metadata updates are enabled by default. `models --refresh`
refreshes the audited active profile; the original goal and criteria remain fixed.

## Evidence packet

Packets are extensible JSON objects. Include enough actual context for a reviewer
working outside the project without editing tools. For example:

```json
{
  "summary": "Implemented the requested parser; malformed input is now rejected.",
  "proposal": "Keep the parser change and add the missing integration check.",
  "evidence": [
    {"path": "src/parser.py", "excerpt": "The inspected changed function or a complete relevant diff."}
  ],
  "checks": [
    {"method": "python3 -m unittest tests.test_parser", "result": "Observed output and exit status", "evidence": "/absolute/path/to/saved-output.txt"}
  ],
  "next_step": "One bounded milestone toward the original success criteria."
}
```

Describe checks that were actually performed. For the initial plan, real inspection
of requirements and existing code is valid evidence; do not invent a test run.
Paths establish provenance; excerpts and actual results supply the review context.

The helper adds the original goal, criteria, stage, round, current artifact hash,
predecessor review hash, previous complete report and response schema. The reviewer
returns the [review report format](../assets/ccloop.review.example.json): all
criteria, checks, claims, issues, reconciliation and a next milestone. Every prior
claim/issue ID and its statement remain present; resolve or retract explicitly.
Use the actual configured criterion IDs, current round and hashes supplied by the
helper. The example is an initial report; later reports retain the full registers.

`plan` reviews the intended approach. `milestone` reviews completed work. `final`
checks whether every original criterion is established and blocking issues are
resolved. Final success is evaluated before the further-call budget gate, allowing
the last permitted consultation to establish completion. A non-final review cannot
declare overall completion.

An exit-zero CLI response can still contain an invalid report. If a review omits
criteria/register entries, uses a stale checkpoint, or fails validation, the
helper blocks it without accepting that review or invoking an access fallback.
Inspect `status`, the recorded failure and last accepted report. Correct the
packet or response-format instructions, then resume with the actual corrective
reason and submit a fresh consultation. The failed attempt still consumes its
round; all previous criteria, claims, issues and budgets remain intact:

```bash
python3 scripts/ccloop.py --root /work/private-supervisor resume \
  --reason 'Corrected the reviewer packet after the recorded invalid report'
python3 scripts/ccloop.py --root /work/private-supervisor consult \
  --packet /work/private-supervisor/corrected-packet.json --stage milestone
```

This routine correction follows the original cross-review authorization. An
actual user stop requires the user's continuation before `resume`; an exhausted
budget cannot be reset by either command. Model substitution detected in trusted
CLI metadata is a selected-model access failure and may use Kimi.

## Routing and access

| Active driver | Primary reviewer | Access fallback |
| --- | --- | --- |
| Claude Code | Codex | Kimi |
| Codex | Claude Code | Kimi |
| Kimi | Claude Code, then Codex on access failure | Stop if neither is accessible |

The helper records intent before the request and preserves actual attempts and
reports. Authentication/quota/model access/service failures and timeouts may use
the designated alternative. An invalid report is an invalid report, not a reason
to disguise the failure by changing providers. Claude model substitution is checked
against returned `modelUsage`; other CLIs record an unknown observed model when
their native output has no trustworthy model field.

The active driver can change through an explicit checkpoint handoff:

```bash
python3 scripts/ccloop.py --root /work/private-supervisor handoff \
  --driver kimi --reason 'Claude service returned an observed access error'
python3 scripts/launch.py --driver kimi --project /work/project \
  --goal 'The original goal' --root /work/private-supervisor
```

Handoff preserves original goal/criteria and budgets. If the prior call remains
in flight, reconcile its state before handoff. If both primary services are down,
Kimi can retain useful work but cannot manufacture an independent second review.

## Stop, recovery and limitations

```bash
python3 scripts/ccloop.py --root /work/private-supervisor stop --reason 'User stop'
python3 scripts/ccloop.py --root /work/private-supervisor resume --reason 'User continuation'
```

Read status after every command. A stopped or exhausted run does not authorize
more work. An interrupted consultation retains its intent; inspect it before
resuming. The helper rejects stale results and user-stop races. It does not turn
two model opinions into proof, sandbox the driver's work, or run after its host
client exits. Native CLI safety and account controls remain in effect.

Kimi consultation uses a zero-tool custom agent, an explicitly empty MCP file,
and an empty skills directory. The helper refuses configured Kimi hooks/plugins
that could reintroduce tools. Python 3.10 may require removing ambiguity in the
Kimi TOML preflight or using Python 3.11+; it does not bypass that check. Reviewer
processes receive supplied context through stdin, not credentials through prompts.

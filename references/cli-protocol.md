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
`prompts/` and `observations/`, with an additional private `ccloop.md` journal.
`status` exposes its `journal_path`. Requests, model attempts and replies are
recorded automatically; follow [journal recording](journal.md) to append the
driver's choice, work, actual checks and final conclusion with
`record --after-round ROUND --entry FILE`. New runs are continuous by default: `max_rounds`,
`max_hours` and `max_no_progress_reviews` are `null`, meaning no automatic cap.
Positive values explicitly supplied by the user still impose their respective
limits; existing ledgers retain their recorded numeric caps. The per-call timeout
remains 300 seconds, and the periodic review interval remains **30 minutes**.
The review interval is separate from an explicitly bounded whole-run time limit
and the per-call timeout.
Status exposes `last_review_at`, `next_review_at` and `review_due`; the active host
checks these while working. Plan and final reviews happen immediately. The
`heartbeat` command prints a durable wakeup prompt but creates no schedule.
Use explicit limits from the user's instruction rather than silently adding a
two-hour or fixed-round stop. Changing a config file does not reset a run.
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

## Immediate questions and decisions

Use `ask` whenever an ordinary question would interrupt progress. It does not
wait for the review interval. The same opposite-model routing, frontier model
profile, frozen goal, artifact checks, process cancellation and Kimi fallback apply:

```bash
python3 scripts/ccloop.py --root /work/private-supervisor ask \
  --question 'Should the parser keep standard-library JSON or add a YAML dependency?' \
  --context /work/private-supervisor/parser-options.md
```

`--context` is optional UTF-8 text, including JSON or relevant source excerpts.
The helper supplies the original goal and current review context. The decision
schema binds `question`, `round`, `artifact_sha256` and `previous_review_sha256`
to this request. Its remaining fields are `answer`, `rationale`, `assumptions`
(list of strings), `requires_user_input` (boolean) and `user_input_reason` (string,
empty when human input is unnecessary).

The recorded `last_decision` is advice for the driver to evaluate and apply. A
question answer does not update the criterion/claim/issue register, declare
completion, reset `next_review_at`, or change the no-progress counter. Questions
increment `question_count` and the dispatch round: an explicit `max_rounds` bounds
all peer requests, including questions. With the default `null` cap, routine
questions do not stop the loop.

Inspect the repository, ask the peer, choose a reversible action, record the
assumptions and execute the next step. If the peer asks for more technical
context, supply it or ask a focused follow-up. Do not turn ordinary uncertainty
into a human confirmation request. `requires_user_input` is advisory; only a
genuinely essential missing private fact, credential or new authorization calls
for the user's input. Peer advice is not that authorization.

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

Read status after every command. A user-stopped or explicitly exhausted run does not authorize
more work. An interrupted consultation retains its intent; inspect it before
resuming. The helper rejects stale results and user-stop races. It does not turn
two model opinions into proof, sandbox the driver's work, or run after its host
client exits. Native CLI safety and account controls remain in effect.

Kimi consultation uses a zero-tool custom agent, an explicitly empty MCP file,
and an empty skills directory. The helper refuses configured Kimi hooks/plugins
that could reintroduce tools. Python 3.10 may require removing ambiguity in the
Kimi TOML preflight or using Python 3.11+; it does not bypass that check. Reviewer
processes receive supplied context through stdin, not credentials through prompts.

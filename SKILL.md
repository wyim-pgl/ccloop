---
name: ccloop
description: Use when a user says ccloop followed by a goal, ccloop clear, or ccloop resume ROOT, or wants continuous Claude Code and Codex collaboration that resolves ordinary questions through the other model, with frontier models, saved recovery files and Kimi access fallback.
---

# ccloop

The current client starts and observes the managed runtime for the user's goal.
Its Claude Code worker asks Codex for review; its Codex worker asks Claude Code.
The runtime applies the review, saves recovery files and starts the next bounded
turn until the original success criteria are established or a stopping condition
is reached. A skill instruction alone does not schedule another turn.
Kimi replaces an inaccessible participant. The other participant still supplies
independent review; Kimi reviewing its own work is not cross-review.

Accept `ccloop <goal>`, `$ccloop <goal>` in Codex, or `/ccloop <goal>` in Claude
Code as an instruction to execute the goal continuously. Start from that goal;
derive practical success criteria and proceed with reversible, goal-consistent
defaults. A plan, an ordinary question, a failed attempt or a finished milestone
is a checkpoint, not a reason to hand the turn back unfinished.
The invocation delegates ordinary planning, implementation and reversible project
choices. When a process skill would request approval of an ordinary design or
next step, obtain peer advice and continue under that delegation. It does not
delegate new account permissions, credentials or unrelated external actions.

If `CCLOOP_REVIEWER` is set, answer only the supplied consultation: the calling
driver owns the loop. If `CCLOOP_MANAGED_WORKER` is set, follow only the supplied
bounded worker task and response schema. The manager owns checkpoints, peer
questions, reviews, continuation and context rotation; return your actual work
and uncertainties to it. For an existing desktop ledger (`schema: 1` with a Claude
UI target), read [the legacy skill](references/legacy-desktop/SKILL.md) instead.

## Default dispatch

For an ordinary goal, **run the manager with an execution tool in this turn**.
Resolve the installed runtime and project first; Windows hosts dispatch inside
WSL using POSIX paths. Prefer the absolute installed wrapper when the shell does
not include the user's local bin directory:

```bash
~/.local/bin/ccloop --driver codex --project /work/project --goal 'The exact user goal'
```

Use `--driver claude` from Claude Code. The manager creates a persistent private
ledger, checks model access, obtains the initial independent review and continues
the work. It does not need the outer client to prepare a plan-only response or
ask routine design approval. Observe the process and its status until verified
completion or an evidenced blocker; starting it is not completion.
If the user supplies an existing root, first read its status/context and pass
`--root` with its exact recorded goal, project and active driver. Prepare a custom
configuration only when the supplied scope needs it, then launch in the same turn.
Only explicit `--interactive` selects the host-driven protocol below.

## Start from a goal

1. Identify the actual host: `claude`, `codex`, or a Kimi handoff. Keep the supplied
   goal and scope intact. Derive concrete success criteria from it. Resolve
   ordinary gaps by inspecting the project and consulting the other model.
   Record the user's authorization and any limits the user actually supplied. Installing
   the skill alone starts no work.
2. Read [models](references/models.md). The helper automatically refreshes the
   strongest model profile using current official metadata and local model lists;
   inspect its sources and any refresh warning. Use the highest suitable
   single-task reasoning setting.
   Check the host's actual model through its client controls. A skill cannot
   change an already running host model. When needed, start the frontier driver
   with `scripts/launch.py`; preserve the goal and ledger in the handoff. Do not
   represent a configured model as an observed model.
3. Resolve this skill folder's real path and use its absolute script paths. Run
   `python3 /path/to/ccloop/scripts/ccloop.py doctor`. Use existing authenticated
   CLIs; there is no desktop UI or scheduler dependency. Missing primary CLI
   access may use Kimi; missing Kimi access is a reported blocker.
   The runtime requires a POSIX environment. A native Windows host must run the
   helper and launcher through WSL, resolving both project and ledger paths inside
   WSL; use the installed `ccloop` wrapper's Python runtime when available. Run
   `doctor` there before dispatch. Do not run these scripts with Windows Python.
4. Read [CLI protocol](references/cli-protocol.md). Normal dispatch lets the manager
   initialize its ledger automatically. For a custom frozen configuration, create
   a private supervisor
   directory outside the project and skill, normally under
   `$XDG_STATE_HOME/ccloop/` or `~/.local/state/ccloop/` with a unique run folder,
   so temporary-directory cleanup does not remove the journal. Initialize from
   [the config template](assets/ccloop.config.example.json). For an existing run,
   inspect `status` and resume its records rather than resetting them.
   Keep its `journal_path`: the helper automatically maintains the private
   `ccloop.md` question/answer journal. Read [journal recording](references/journal.md)
   before the first driver decision or work checkpoint.
5. **Start the manager for every normal goal invocation.** Use the same root,
   frozen project/goal and recorded active driver:

   ```bash
   python3 /path/to/ccloop/scripts/launch.py --root /private/run \
     --project /work/project --driver codex --goal 'The exact frozen goal'
   ```

   Managed execution is the default; `--managed` remains an explicit alias.
   Select the matching Claude driver for a Claude run. On an existing run use
   its recorded driver, without silently handing it off to the current host.
   Observe the manager process and read `status` while it works. Keep waiting
   through bounded turns until independently verified completion or an actual
   stopping condition. Report the real failure if it exits; an exit-zero process
   alone is not proof of goal completion. Do not run a second driver, duplicate
   its project work, or finish merely because the manager started.
   Use [managed context recovery](references/context.md) for details. Only an
   explicit request for `--interactive` selects the host-driven protocol below.

## Explicit interactive mode: work, ask, verify, continue

This protocol applies when the user explicitly chose `--interactive`. In normal
managed runs the manager owns these steps; the outer client observes its result.

1. Before substantial work, prepare a JSON packet containing the proposed plan,
   relevant source excerpts, constraints and checks actually performed. Consult
   the opposite CLI with `--stage plan`. The helper supplies the original goal,
   criteria, previous review and current artifact hashes automatically.
2. Assess the objections against the original goal. Repair invalid assumptions
   before expanding the work. The reviewer gives advice; the driver remains
   responsible for deciding, implementing and verifying the next bounded step.
   Record the choice before acting, then append the actual actions and checks
   using the journal's `record` command; reference the originating consultation
   round explicitly, including when newer questions have since been answered.
   When a question would otherwise interrupt work, use **Ask the other model**
   below immediately, then act on a justified answer and continue.
3. Review progress every **30 minutes by default**. Read `review_due` and
   `next_review_at` from status while working. The initial plan and final review
   happen immediately; material contradictions or user requests can justify an
   earlier review. At a due review, run the relevant checks and save a fresh
   packet with actual commands, results, changed excerpts/diffs and unresolved
   objections. Use `--stage milestone`. Reviewer tools are disabled or restricted
   and the review runs outside the project, so a file path alone is not its contents.
4. Read the returned report and state before continuing. Carry forward claims
   and issues, explicitly correcting or resolving them. Unknown criteria and open
   blocking issues keep the goal unfinished. Keep numerical evidence distinct
   from proof and a proposed check distinct from an executed check.
5. Continue the next bounded step without seeking routine confirmation. Do not
   finish with a plan, a menu of ordinary choices, "should I continue?", or an
   offer to do the next step later. Reconcile new evidence against earlier
   decisions. Artifact changes during consultation invalidate that result;
   review the current state before acting on it. Repair recoverable command or
   report errors, record the corrective reason, resume and retry within scope.
6. When all original criteria appear satisfied, run the final verification and
   consult with `--stage final`. Finish only when the helper records `complete`:
   every original criterion is met with evidence and no blocking issue is open.
   A plan or milestone completion does not finish the overall goal.
   Before ending, append the final work, verification results and user-facing
   conclusion to the journal, and give the user its absolute `journal_path`.

In explicit interactive mode, record checkpoints often enough that another participant can continue from the
last verified work. After a bounded step or a material decision, use the structured
`checkpoint` command in [context recovery](references/context.md) to save current
notes, completed work, next steps and quarantined claims. The helper maintains
`intend.md`, `resume.md`, `todo.md` and `quarantine.md` from the durable ledger.
On resuming after compaction or a session change, run `context` and read the
actual restored content before work.
In managed mode the runtime owns these writes. The outer client observes status
and must not append work/checkpoints or drive project changes concurrently.
Existing interactive clients rely on their host's session lifecycle; managed mode
can replace its worker sessions while its manager process stays alive. Neither
mode installs a background service. A closed manager does not continue by itself.
Check due status at normal tool boundaries; use nonblocking job observations for
long calculations so a single blocking wait does not hide the 30-minute review.
The helper's `heartbeat` prints the durable prompt for a supported scheduler;
printing it does not schedule execution.

## Ask the other model

Technical uncertainties, design choices, reversible preferences, failed checks
and disagreements go to the peer, rather than becoming questions to the user.
Inspect available project evidence first, save the relevant excerpts/options in
a context file, and run:

```bash
python3 /path/to/ccloop/scripts/ccloop.py --root /private/run ask \
  --question 'Which next step best advances the original goal, and why?' \
  --context /private/run/question-context.md
```

The helper routes the question through the same frontier-model/Kimi policy and
records the answer, rationale and assumptions in `last_decision` and the ledger.
Evaluate the answer against the original goal, choose an authorized action,
record why in `ccloop.md` through `record --after-round ROUND --entry FILE`,
then execute it and record the actual outcome. Use the returned decision's round;
the helper does not infer which answer a delayed work entry belongs to.
If the peer needs technical clarification, obtain the
missing evidence or send a focused follow-up question to the peer. Resolve
ordinary disagreements with a reversible choice and an actual check.

Questions do not wait for the 30-minute review interval, reset that interval or
establish success criteria. Final completion still requires final cross-review.
Peer advice cannot supply the user's private facts, credentials or new authority.
Treat `requires_user_input` as advisory: verify the supposed missing fact or
permission is essential and genuinely unavailable before interrupting the user.
Complete any independent preparation first, then ask only for that necessary item.

## Clear the context

`/ccloop clear` in Claude Code, `$ccloop clear` in Codex, or `ccloop clear` in the
terminal asks the driver to save the run, obtain a peer review of the saved
recovery files, and then restart with an empty context. A skill cannot clear the
host chat itself, so the restart is a user step that the driver prepares:

1. Write a checkpoint packet with `summary`, `next_steps`, `completed`,
   `working_notes` and `quarantine`: superseded assumptions, refuted claims,
   abandoned approaches and stale numbers that a fresh context must not reuse.
2. Run `python3 /path/to/ccloop/scripts/ccloop.py --root /private/run clear
   --packet FILE --reason 'User requested a context clear'`. The helper saves the
   checkpoint, regenerates `resume.md`, `intend.md`, `todo.md` and
   `quarantine.md`, sends their actual text to the opposite model as a
   `--stage handoff` review, and records the accepted review and the restore
   command in the ledger and `ccloop.md`. A rejected review leaves the
   checkpoint saved and exits nonzero; fix the packet and run `clear` again.
3. Report the review verdict, any issues it raised, and the `restore` lines
   from the result. Tell the user to run `/clear` (Claude Code) or `/new`
   (Codex) and then enter `/ccloop resume /private/run` or
   `$ccloop resume /private/run`. The terminal restore line starts the frontier
   driver with the same root through `scripts/launch.py`.

While a managed runtime owns the run, `clear` only records a rotation request;
the manager starts a fresh worker context at its next accepted turn and logs the
request in the ledger. Do not clear during an in-flight consultation.

## Resume an existing run

`/ccloop resume /private/run` or `$ccloop resume /private/run` continues an
existing ledger in a fresh context. Run `status` and `context`, read all four
restored files and honor stopped, blocked, complete or exhausted status. Reconcile
saved artifact identities with the actual files. Start the same managed launcher
using the recorded goal, project and active driver, then observe it through
completion. This invocation does not authorize resetting a run or resuming an
explicit user stop. Treat `quarantine.md` entries as not reusable without new
evidence. Driver notes are reports; accepted independent reviews establish proof.

## Access failures and stopping

The helper attempts the chosen primary reviewer, then Kimi once for an executable,
authentication, model-access, usage/rate, endpoint or timeout failure. It records
both attempts and their models. Invalid arguments, malformed reviews, content
refusals and failed project tests are distinct from service access failures.
Treat them according to their actual cause. A hidden weaker-model substitution
does not satisfy the selected frontier-model requirement.

If the **driver** loses access, use the same saved ledger in Kimi, run
`handoff --driver kimi --reason 'Observed access failure'`, and retain the original
goal, criteria and remaining budgets. The launcher can handle a missing driver
executable; mid-session service interruption may require starting the Kimi client
from the terminal. Kimi uses an accessible Claude/Codex reviewer. If no independent
reviewer is accessible, save the checkpoint and report the blocker.

New runs have no automatic round, elapsed-time or no-progress cap by default;
the corresponding config values are `null`. Keep working until verified goal
completion, explicit user stop, a user-supplied limit, or an evidenced blocker
that the available participants cannot resolve. A difficult problem or repeated
failed approach calls for a different peer-assisted step, not an arbitrary stop.
Honor `stop` immediately. Preserve explicit limits and existing ledgers' numeric
limits; never silently discard them. An incomplete stop is not success. Resume
and handoff preserve the original run and do not replay uncertain in-flight work.
Consultation is bounded by the run's configured timeout and uses no editing role.
Project actions still follow the user's scope and normal client permissions.

Report the completed work, actual verification, reviewer/model evidence and any
remaining blocker. Keep real transcripts, prompts and research artifacts private.

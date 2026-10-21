---
name: ccloop
description: Use when a user gives a goal and wants Claude Code and Codex to cross-review progress until completion, with frontier models and Kimi fallback when either service is inaccessible.
---

# ccloop

The current client drives the user's goal. Claude Code asks Codex for review;
Codex asks Claude Code. Apply the review, verify the result, and continue until
the original success criteria are established or a stopping condition is reached.
Kimi replaces an inaccessible participant. The other participant still supplies
independent review; Kimi reviewing its own work is not cross-review.

If `CCLOOP_REVIEWER` is set, answer only the supplied consultation: the calling
driver owns the loop. For an existing desktop ledger (`schema: 1` with a Claude
UI target), read [the legacy skill](references/legacy-desktop/SKILL.md) instead.

## Start from a goal

1. Identify the actual host: `claude`, `codex`, or a Kimi handoff. Keep the supplied
   goal and scope intact. Derive concrete success criteria from it; resolve only
   material gaps. Record the user's authorization and bounded limits. Installing
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
4. Read [CLI protocol](references/cli-protocol.md). Create a private supervisor
   directory outside the project and skill, and initialize from
   [the config template](assets/ccloop.config.example.json). For an existing run,
   inspect `status` and resume its records rather than resetting them.

## Work, ask, verify, continue

1. Before substantial work, prepare a JSON packet containing the proposed plan,
   relevant source excerpts, constraints and checks actually performed. Consult
   the opposite CLI with `--stage plan`. The helper supplies the original goal,
   criteria, previous review and current artifact hashes automatically.
2. Assess the objections against the original goal. Repair invalid assumptions
   before expanding the work. The reviewer gives advice; the driver remains
   responsible for deciding, implementing and verifying the next bounded step.
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
5. Continue the next bounded step without seeking routine confirmation. Reconcile
   new evidence against earlier decisions. Artifact changes during consultation
   invalidate that result; review the current state before acting on it.
6. When all original criteria appear satisfied, run the final verification and
   consult with `--stage final`. Finish only when the helper records `complete`:
   every original criterion is met with evidence and no blocking issue is open.
   A plan or milestone completion does not finish the overall goal.

Record checkpoints often enough that another participant can continue from the
last verified work. This is an active-session loop, not a background scheduling
service. A closed client does not keep executing work by itself.
Check due status at normal tool boundaries; use nonblocking job observations for
long calculations so a single blocking wait does not hide the 30-minute review.
The helper's `heartbeat` prints the durable prompt for a supported scheduler;
printing it does not schedule execution.

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

Honor `stop` immediately. Stop on verified completion, exhausted round/time limits,
repeated lack of verified progress, or an unresolved access/blocking condition.
An incomplete stop is not success. Resume and handoff preserve the original run;
neither resets its budgets nor authorizes replay of uncertain in-flight work.
Consultation is bounded by the run's configured timeout and uses no editing role.
Project actions still follow the user's scope and normal client permissions.

Report the completed work, actual verification, reviewer/model evidence and any
remaining blocker. Keep real transcripts, prompts and research artifacts private.

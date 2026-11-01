---
name: decision-triage
description: >-
  Sorts a ptr727/ProjectTemplate fleet repository's open issues into those an agent can fix alone
  and those that need the maintainer, researches a recommended answer for each question, files
  each one as a `decision` issue, then puts every question to the maintainer in one sitting and
  records the answers. Use this whenever asked what needs the maintainer's input, to find the
  open questions, to get the backlog ready for an unattended run, to clear the decision queue, or
  to ask everything at once before leaving. Triggers even when the backlog looks like it has no
  questions in it, because a question no session has filed reads to every later run as ordinary
  work. An unattended run then either guesses at it or skips it and reports that it needs the
  maintainer without saying what for. Attended only, since its last step asks the maintainer.
  It owns the "Needs the Maintainer" criteria, which `backlog-burndown` carries as a generated
  include. It runs before a `backlog-burndown` away-mode run and does not replace one. It
  presents the whole queue, where `session-handoff` "The Parked Decision Queue" presents the
  top of it at a session close.
---

# Decision Triage

## Why This Exists

An unattended run works only what needs no decision. It can only skip a question it meets, or
park the work behind one. Either way the maintainer learns of the question after the run, one
question at a time, with no answer researched. This skill moves that work before the run. It
finds every open question, researches a recommendation for each, and asks them all while the
maintainer is present. The run that follows then has the answers and meets fewer questions.

## The Seats

- **The triage seat** is the session this skill runs in. It reads the issue list, dispatches the
  classifiers and researchers, writes the `decision` issues, and asks the maintainer. It reads no
  issue body itself beyond what a subagent returns.
- **A classifier** is a subagent given a batch of issue numbers. It returns one line per issue.
- **A researcher** is a subagent given one question. It returns the question, its options, and
  the evidence.

## Scope

One repository, the one the session is in, resolved from its own `origin`. A fleet-wide triage is
a different request. Every write here is an outward-facing write bound by `GOVERNANCE.md`
"Repository Boundaries and Write Safety". So it targets this repository only, reads every
identifier live in the same run, and suppresses no output.

## Needs the Maintainer

An issue needs the maintainer when any of the following holds. Where it is unclear whether one
holds, the issue needs the maintainer. A question asked costs one line of the maintainer's time,
and a guess costs a revert and a review round.

- **The outcome is not determined.** The issue asks a question, or names alternatives without
  choosing one, and the committed rules do not choose for it. An outcome the issue and the rules
  together settle needs no one, however large the fix.
- **It waits on the maintainer already.** It carries the `decision` label, or a comment asks the
  maintainer something still unanswered.
- **The fix reaches past the repository's tree.** It changes a repository setting, a ruleset,
  visibility, a secret, or a release condition. Or it needs a credential, an account, or a host
  the session lacks.
- **The fix edits anything under the hub's `host-setup/agent-safety/`.** That tree is the agent's
  own guard, so a human approves every change to it.
- **The fix reverses a settled decision** recorded in an issue, a handoff, or the rule text.
- **The fix adds a rule the issue does not ask for.** A rule a future agent must honor is the
  maintainer's to judge. An issue that asks for a rule has been judged, and one that only reports
  a defect has not.

An issue where only part needs the maintainer is a split rather than a whole. The part that needs
no one is worked, and the undecided part is filed as its own `decision` issue. Skipping the whole
issue for one undecided item is the failure this rule exists to prevent.

These are not reasons an issue needs the maintainer, and a report never counts them as such. An
issue that is `blocked` on another issue waits on that issue. One that needs a platform, by the
test `session-handoff` "The Chain" states, waits on that platform's lane. One that another group
or pull request holds waits on that work. One carrying `canonical-sweep` belongs to the workflow
that owns it. A handoff link carrying the `handoff` label is not an issue of its own.

## The Procedure

1. **Read the open issues** with `gh issue list --repo "<owner>/<repo>" --state open --limit 500
   --json number,title,labels,updatedAt`. A read returning as many rows as its limit may be
   truncated, so raise the limit and read again until one returns fewer. Leave out every issue
   carrying `handoff` or `canonical-sweep`. Keep the open `decision` issues aside as questions
   already queued.
2. **Classify the rest** in batches, one `standard` classifier per batch, at most four running at
   once. Each applies "Needs the Maintainer" above, which its brief quotes whole. It returns one
   line per issue in the form "Return Lines" below.
3. **Research each question**, one `frontier` researcher per question, at most four running at
   once. That covers each `ask` and `split` line and each queued `decision` issue that states no
   options. A researcher reads the issue, the rule text it touches, and the code. It returns the
   question, its options, and its evidence, in the form "Return Lines" below. It writes nothing.
4. **Record each question** before asking it, so the question survives an interrupted session.
   - For an `ask`, comment the researched question on the issue and add the `decision` label.
   - For a `split`, file a new issue holding only the undecided item, carrying `decision`.
     Comment on the original issue naming the new one and the part that stays with it.
   - For a queued `decision` issue, comment the researched options where it stated none.

   The comment states the question, then the options, the recommended one first and marked as the
   recommendation, each with its reason. It names the evidence and what each option does to any
   work in flight, per `GOVERNANCE.md` "Communicating with the User".
5. **Post the queue, then ask it.** First post one message listing every question with its issue
   as a clickable link, since a prompt's own text links nothing. Then put the questions through
   the interface's prompt mechanism, one question per decision, its options as the choices. Fill
   each prompt with as many questions as it carries, and repeat until the queue is empty. Order
   them by what blocks work in flight first, then by longest waited. Where no prompt mechanism
   exists, the questions go as one numbered list.
6. **Apply each answer** as it arrives.
   - Comment the answer on the issue and take the `decision` label off.
   - Close the issue where it held nothing but the question, as a `split` issue does.
   - Where the answer settles the work, leave the issue open as work. An away run then fixes it.
   - Where an answer unblocks a parked group, the parking comment names the group's issue. Comment
     on that issue that its decision is answered. Leave its `blocked` label for the worker that
     resumes it, per `GOVERNANCE.md` "Durable Knowledge and Self-Improvement".

   A question the maintainer skips keeps its label and stays in the queue.
7. **Report** the counts: issues an agent can fix alone, questions answered, questions still
   open, and splits filed. Also name each issue held back for a reason "Needs the Maintainer"
   does not count, under that reason. End with one line saying a `backlog-burndown` away-mode run
   can now work the rest.

## Return Lines

| Seat | Line | Means |
| --- | --- | --- |
| classifier | `AUTO #<n>` | an agent can fix it alone |
| classifier | `ASK #<n> <the question in one sentence>` | the whole issue needs the maintainer |
| classifier | `SPLIT #<n> <the undecided item in one sentence>` | part needs the maintainer, the rest does not |
| classifier | `HELD #<n> <reason>` | waits on something other than the maintainer |
| researcher | a block holding `QUESTION`, then `OPTION` lines, recommended first, each with its reason, then `EVIDENCE` | the researched question |

A reply in no form above is not read further. Its issue is classified again in the next batch, or
reported as unclassified where it fails twice.

## Briefs

Brief on `AGENTS.md` "Context and Delegation Discipline"'s subagent shape. A classifier's brief
quotes "Needs the Maintainer" whole and lists its issue numbers. A researcher's brief names the
issue, the question, and the rule files the issue touches. Each brief states that the subagent
writes nothing. It also states that the subagent stops and reports where a rule it was given does
not cover what it finds.

## Model Tiers

`AGENTS.md` "Model Tiers" keeps the tier rule, carried here whole as a generated include. A tier
this skill names then resolves to its model even where a carried `AGENTS.md` predates the table.

<!-- include: AGENTS.md > Model Tiers -->

A rule, a Skill, or a brief names a model tier, never a model. A model release is then an edit to the table below and to the hub's `spec/model-tiers.json`. That file states the same mapping as data, and the hub's spec gate holds the two equal. Reasoning effort is a separate axis stated beside the tier, such as `bulk` at low effort.

- **`frontier`** is the fleet's default reasoning tier, for orchestrating, rule text, design, spec logic, and adversarial review. It is not the most capable model available, which is `highest`.
- **`standard`** is ordinary implementation and driving a pull request. Judging a review finding is never mechanical, however often it repeats, so it runs at `standard` or above. A `standard` driver's evidence-backed decline of a finding raised on its pull request stands, since that pull request's reviewers and its merge gate check it.
- **`bulk`** is the cheapest model that does the work correctly. It is for high fan-out mechanical work, such as an extraction or a status check.
- **`highest`** is the most capable model available, and only the maintainer selects it. The maintainer chooses it for the session itself, or allows it for the session's subagents at launch. The host's agent-safety kit reads that launch setting, `FLEET_ALLOW_HIGHEST=1` on Claude Code. Naming the tier in a prompt does not set it. A maintainer naming it partway through a session restarts the session with it allowed, and an agent never sets it. Without that setting, the kit refuses a dispatch that names it. A dispatch naming no model runs at its agent definition's model where one is set, and otherwise at the session's own. A fork always runs at the session's model, whatever it names. So a session launched on `highest` names a tier on every dispatch meant to run below it, and uses no fork for such work. A session never reaches for it because a task looks hard.

| Tool | `highest` | `frontier` | `standard` | `bulk` |
| --- | --- | --- | --- | --- |
| Claude Code | `fable` | `opus` | `sonnet` | `haiku` |

Each cell is the name that tool's subagent dispatch takes, which follows the latest model of its family. A dispatch on a tool with no row here, or on one that cannot name a model per subagent, names no model.

- **A tier resolves only through this table.** A word such as "frontier", "strongest", or "most capable" never resolves to `highest`. The industry's "frontier model" is the opposite of what this tier means here. A session that cannot see this table names no model rather than guessing one.
- **An orchestrating seat runs at `frontier` and names a tier on every dispatch.** It defaults a subagent to `frontier`, and picks `standard` or `bulk` deliberately for execution or mechanical work.

<!-- /include -->

## Mechanics Live Elsewhere

- How a question is put to the maintainer, and when an item leaves the queue: `GOVERNANCE.md`
  "Communicating with the User".
- The parked decision queue a session close presents: `session-handoff`.
- Working the issues that need no one, attended or away: `backlog-burndown`.

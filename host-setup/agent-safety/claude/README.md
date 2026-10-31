# Claude Code Host-Safety Kit

This is the Claude Code implementation of the host-safety spec at [`../README.md`][spec].
Its guards are per-machine, user-account-scoped, and deployed first on any system where Claude Code uses the
maintainer's `gh` credentials or runs unattended. Linux, WSL, macOS, and Windows are supported. See the spec for the
requirements this kit satisfies and why each exists. What follows here is Claude-Code-specific
installation and operational detail. Codex and opencode have no equivalent hook yet -- see
[`../codex/README.md`][codex] and [`../opencode/README.md`][opencode] for their status, tracked at
[issue #781][issue-781].

## What It Installs

Into `~/.claude/` (or `%USERPROFILE%\.claude\` on Windows):

- **`hooks/gh-write-guard.py`**: a PreToolUse hook that denies the plain shapes of four classes of dangerous action. First, the GitHub **write** footguns behind the cross-repo comment incident. A GitHub write whose output is discarded or forced to success is one. A GraphQL mutation passing a **literal** node id instead of a `$variable` is another. So is a `gh` write whose explicit target is under an owner other than the checkout origin's. Sibling repositories under the same owner are allowed. That is because the harm this guards is reaching a stranger's repository rather than working across one maintainer's own fleet. A different owner is allowed only when the maintainer names it in `GH_WRITE_GUARD_ALLOW` (an `owner/repo` list, where `owner/*` grants a whole owner). That variable is read from the environment the session was launched with, which a tool call inside that session cannot change. A hook runs as its own process, so an inline `VAR=x cmd` prefix or an `export` inside a Bash call never reaches it. Second, a **git operation that bypasses a repository protection**. `gh pr merge --admin` is one. A commit or push that skips the hooks by `--no-verify` or by a `core.hooksPath` or include override is another. So is a push the branch's **live** rules forbid. So a code-style `develop` is denied while a config-style `develop` is allowed with no per-repo configuration. A push to a protected-default branch fails closed when its rules cannot be determined. Third, a **mutating git operation run directly against a primary checkout**. That is the harm behind a second incident. In it an agent reused the maintainer's own primary checkout instead of a worktree. The agent did so despite having read the prose rule against it. `GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT` grants an exception through the same channel as `GH_WRITE_GUARD_ALLOW`. Fourth, an **unbounded shell wait**, a loop that sleeps with no bound. That is the harm behind a third incident. In it seven such loops outlived the subagents that wrote them and the run that dispatched those subagents. The hook reads a command with one simple lexer and denies a shape only where it appears plainly. A shape it does not read is allowed, per the root spec's threat model. It fires even in autonomous / bypass-permissions sessions, which is how the first incident happened. See the root spec's requirements 1 to 7 for the exact rules.
- **`hooks/tool-containment.py`**: a shell prefix that runs every Bash tool call inside a transient systemd user scope carrying `TasksMax`, `MemoryMax`, and `MemorySwapMax=0`. The last is there so a runaway at the memory ceiling is killed rather than spilled into the host's swap. The prefix is registered as `CLAUDE_CODE_SHELL_PREFIX` in the `env` block of `settings.json`. A runaway fan-out then fails at the ceiling with `fork: Resource temporarily unavailable` instead of growing until the host has to be reset. That is the harm behind the probe-shim incident in the root spec's requirement 9. The guard above reads the command text, and that incident's recursion lived in a script file the command only named. The ceilings default to 8192 tasks and 25% of RAM. A `claude` session started from a tool call is nested in the session that ran it. Every scope of such a chain also joins one slice, `claude-tree-<root session>.slice`. That slice carries a total ceiling for the whole tree, 32768 tasks and 50% of RAM. The root's id reaches nested sessions as `AGENT_CONTAINMENT_ROOT`, which every contained command inherits. The tree's ceiling is a runtime drop-in put on when its slice is first created, and the sweep below reverts it. The maintainer raises any ceiling in the same `env` block. An inline `VAR=x` in a command cannot change one, since the prefix reads it before the command runs. The variables are `AGENT_CONTAINMENT_TASKS_MAX` and `AGENT_CONTAINMENT_MEMORY_MAX` for a scope, and `AGENT_CONTAINMENT_TREE_TASKS_MAX` and `AGENT_CONTAINMENT_TREE_MEMORY_MAX` for the tree. A tree ceiling set below a scope's binds first. Each scope is named `claude-tool-<session>-<pid>-<random>`, so the sweep below can stop one session's tool calls and nothing else. Its description records the pid of the agent that ran it. That record is how the sweep tells a scope whose session has ended from one whose session still runs. The installer sets the prefix only where it can contain, per the Linux bullet under "Scope and Limits". It never sets it over a `CLAUDE_CODE_SHELL_PREFIX` value that names anything else.
- **`hooks/stray-process-sweep.py`**: a SessionEnd hook in two phases. First, it stops every `claude-tool-<session>-*` scope this session left loaded and reports what each held, since the scope's name proves which session started it. It then stops every scope in this session's tree whose recorded agent is no longer running, and names each one's session. That is how a nested session's scopes end, since stopping the parent's scopes kills the nested `claude` before its own sweep runs. Each stop can end an agent nested deeper, so it judges again, up to four rounds. The root session then stops its tree's slice and reverts the ceiling put on it, once nothing is left in it. Second, it reports, and never ends, the process trees outside those scopes that outlive the session. The guard above stops a leak from being written. This finds the ones already running: the ones started before the guard was installed, and the shapes the guard deliberately does not reach. It reports every descendant of the agent process running outside the agent's own session. It names each subtree root's command and age, and hands over the `kill` line. This phase kills nothing itself. A long build backgrounded on purpose reads identically to a leaked wait from there. What is still wanted on the machine is the maintainer's call. A SessionEnd hook cannot block, and its JSON output is discarded, so stderr on exit 2 is the one channel that reaches the maintainer. The sweep uses that for a report rather than for a refusal. It registers under a group carrying no matcher, which fires on every exit reason. It also carries an explicit timeout, since the event's own budget is 1.5 seconds and only a declared per-hook timeout raises it. See the root spec's requirement 8.
- **`hooks/model-tier-guard.py`** and **`hooks/model-tiers.json`**: a PreToolUse hook that denies a subagent dispatch at the maintainer-only model tier, today `fable`. It reads the tier mapping from the `model-tiers.json` beside it, a copy of the hub's `spec/model-tiers.json` the installer makes. It checks an `Agent` call's `model` and a `Workflow` script's `model` options, from the inline `script` and the file at `scriptPath`. A saved workflow named by `name` alone, or a child workflow a script starts, cannot be inspected and is allowed. A dispatch naming no model runs at a model the hook cannot read, and so does a fork naming another model, so both are allowed. The maintainer allows the tier by launching the session with `FLEET_ALLOW_HIGHEST=1`, which a tool call inside that session cannot change. Any other value stays denied, and the deny text never suggests setting it. Where the maintainer asked for the tier, the deny text says to stop and report rather than dispatch lower. Otherwise it names the default tier's model. A mapping that is missing or unreadable denies any call naming a model and says to re-run the installer, so the guard never goes quiet. A session launched with the allowance blocks no model, so it needs no mapping. It registers in its own `Agent|Workflow` matcher group rather than the Bash one.
- **The whole of `CLAUDE.md`, rendered on every run**, loaded into every session on the machine (including ad-hoc work outside any project). It holds a header, two marker-delimited blocks of host-wide content, and the host-local file. Nothing else survives an install. Content outside the blocks was never written or checked, and it restated fleet rules in wording the fleet had since changed.
  - The host-local file is `~/.config/agent-fleet/local.md`, under `XDG_CONFIG_HOME` where that is set to an absolute path. The installer appends it under a `host-local` marker and never edits it. It is one file for every agent, so a Codex or opencode global file rendered later appends the same text.
  - The stamp records a digest of the file each install wrote. A file matching it is an earlier render and is replaced silently. Any other file was edited by hand, or predates whole-file ownership, so it is copied to `CLAUDE.md.<UTC time>.bak` first and the install prints that path. `--report` names which of the two it found.
  - A local file that cannot be read, is not a regular file, or is not UTF-8 stops the install before anything changes. So does one carrying this kit's own markers, since appending it would duplicate a block.
  - The `agent-safety` block carries restrictions alone, so nothing in it can widen a permission. `Repository Boundaries and Write Safety` mirrors the committed `GOVERNANCE.md` section of the same name. It carries as behavioral guidance its write rules. It carries as behavioral guidance its rules that a task works in its own checkout. It carries as behavioral guidance its rules that a task isolates into its own worktree before its first file edit. `Data in Agent-Authored Text` mirrors `GOVERNANCE.md` "Representative Data in Agent-Authored Text", so an agent illustrates with constructed data rather than data observed on the machine. `Authorization Scope and Memory Hygiene` bounds how a granted permission is recorded and read. Each mirrors a rule that otherwise reaches fleet repos only.
  - The `fleet-bootstrap` block carries `Fleet Governance Entry Point`, which names the template repository and routes by the state a repository is actually in. It is separate precisely because it enables rather than restricts. It is also separate because the safety block's own text says nothing in it widens a permission. Merging the two would contradict that. It is host-wide rather than per repository. That is because the repositories that most need it are the ones carrying no instruction set to point the way. It mirrors the byte-locked `AGENTS.md` "Fleet Bootstrap" section that a conformant repository carries.
- **The permission rules this kit owns, merged into `settings.json`** beside the hook registration. Each is declared as a prefix and a rule. A re-run drops every rule the prefix owns before adding the current one. So a rule whose spelling changes updates in place rather than accumulating beside the version it replaced. Ownership requires a rule-syntax delimiter after the prefix. That is because the prefix ends at the script name and a bare prefix test would also claim a longer path such as `pr_review.py-custom`. With the delimiter required, a rule written by hand for a different script is never touched. These widen rather than restrict. That is why they are their own component, for the same reason the `fleet-bootstrap` block is separate from the `agent-safety` one. Today the list holds the rules for `scripts/pr_review.py`, one for each spelling it runs under, `python3` and `uv run --no-project --script`. They cover the review loop's `reply` and resolve and the `cleanup` delete of a merged pull request's head branch. Driving that loop by hand needs a raw GraphQL mutation carrying a node id, the shape that reached a stranger's repository. The script queries the id itself and takes no argument an id fits in. The `:*` rule approves any arguments, so it approves any `cleanup` call. Every check the delete makes therefore lives inside the subcommand rather than in the rule. What the rule decides is which command runs without a prompt. It matches the command text rather than the directory the command runs in. So it reaches a `scripts/pr_review.py` in any checkout that carries one. An absolute path would not narrow that. That is because the hub is reached as a checkout of the caller's own and its location differs per task. So pinning one path would name a checkout the next task does not use. What bounds it is the rule that an agent reaches the hub as a checkout of its own, fetched immediately before it is read. That rule excludes a copy the agent happens to find on disk. The `fleet-bootstrap` block beside this carries that rule, and [`GOVERNANCE.md`][governance] "Hub-Hosted Tooling" states it in full.
- **The `autoMode` rules this kit owns, merged into `settings.json`** in the same pass. Claude Code reads `autoMode` from user or managed settings alone and never from a repository's `.claude/settings*.json`. So the block is host-wide, and with nothing owning it, it drifted on one machine into one repository's rules applied everywhere. The kit owns an entry by a leading `[agent-safety] ` tag, one constant in `install.py`, rather than by its text. Each run removes every tagged entry from `environment`, `allow`, `soft_deny`, and `hard_deny`, then appends the current ones from one table. An entry without the tag is the machine's own, such as a host's rules for how agents treat a USB device. It keeps its exact text and its place, and the kit never removes, reorders, or rewrites it. The owned rules say that GitHub source control under the owner of each session checkout's origin remote is trusted. They say that `develop` and `main` are ruleset-protected and that a promotion to `main` is the maintainer's. An agent merges a feature branch into `develop` through `scripts/pr_review.py merge`, which checks the Merge Gate's mechanically readable items in code. The findings a pull request's body lists are the agent's to dispose of before that call, since no code reads them. They allow running `scripts/pr_review.py merge` or `cleanup` against a repository under that owner. `merge` checks those gate items before it merges, and `cleanup` verifies the merged state before it deletes anything. The kit owns nothing in `soft_deny` or `hard_deny`, and it never touches `classifyAllShell`. The literal `"$defaults"` splices Claude Code's built-in rules into a list, and a list without it replaces them. So a list the kit creates starts with `"$defaults"`. A list that already exists without it is the machine's choice, which the kit leaves alone and names in the install's output. A list emptied of retired tagged rules down to `"$defaults"` alone reads as one the kit created, so it is dropped. A malformed shape is refused the way a malformed `hooks` or `permissions` key is, with the file left unchanged. That covers `autoMode` as something other than an object and a list as something other than a list. An entry that is not a string carries no tag, so it is kept untouched as the machine's own. The owned rules are in the stamp's digest, so `--report` says stale when the machine's tagged entries differ from what the checkout writes. `--report` also counts the untagged entries in each list and says whether the list carries `"$defaults"`, and it judges none of them.

See [`../README.md`][spec] "Requirements" for which of these each rule implements. See [`../README.md`][spec] "Auditing an
Implementation Against This Spec" for how to check this hook still satisfies them after a change.

## Install (Idempotent, Safe to Re-Run to Update)

```sh
# Linux / WSL / macOS
host-setup/agent-safety/claude/install.sh
```

```powershell
# Windows - the .\ prefix is required, PowerShell does not run a script from a relative path without it
.\host-setup\agent-safety\claude\install.ps1
```

Both are thin wrappers around `install.py`, so every OS runs one tested code path. The installer self-tests the hook before registering it. It merges the settings.json hook entry, the permission rules, and the tagged `autoMode` rules without clobbering other keys. It renders CLAUDE.md whole, backing up a file edited since the last install before replacing it. The settings file is read once and written once, so the hook, the permission rules, and the `autoMode` rules land together or not at all.

**Restart Claude Code sessions on the machine afterward** so the new hook and CLAUDE.md load.

## Refreshing After an Upstream Change

The deployed copy on each machine is a snapshot. So when the guard changes upstream (a new rule or a fix), every machine keeps running the old hook until it is refreshed. The installer **is** the refresh: pull the latest template and re-run `install.sh` (or `install.ps1`) on each machine. It re-copies the hook, re-runs the self-test, and re-registers in place, so a re-run is safe and updates the deployed copy. [#365][issue-365] tracks the per-machine rollout and its re-runs.

## Verify (POSIX Shell)

```sh
python3 ~/.claude/hooks/gh-write-guard.py --selftest        # decision matrix: all cases pass
python3 ~/.claude/hooks/stray-process-sweep.py --selftest  # reporting matrix: all cases pass
python3 ~/.claude/hooks/tool-containment.py --selftest     # containment matrix: all cases pass
python3 ~/.claude/hooks/model-tier-guard.py --selftest     # tier matrix: all cases pass
python3 ~/.claude/hooks/stray-process-sweep.py --orphans   # exit 0: no command scope outlived its session
grep -c CLAUDE_CODE_SHELL_PREFIX ~/.claude/settings.json      # expect 1 where the installer reported the prefix set
grep -c 'agent-safety v' ~/.claude/CLAUDE.md            # expect 2 (start + end marker)
grep -c 'fleet-bootstrap v' ~/.claude/CLAUDE.md         # expect 2 (start + end marker)
grep -cF 'Bash(python3 scripts/pr_review.py:*)' ~/.claude/settings.json   # expect 1 (never duplicated)
grep -cF 'Bash(uv run --no-project --script scripts/pr_review.py:*)' ~/.claude/settings.json   # expect 1
```

On Windows PowerShell:

```powershell
py -3 "$env:USERPROFILE\.claude\hooks\gh-write-guard.py" --selftest        # all cases pass
py -3 "$env:USERPROFILE\.claude\hooks\stray-process-sweep.py" --selftest  # all cases pass
py -3 "$env:USERPROFILE\.claude\hooks\tool-containment.py" --selftest     # all cases pass
py -3 "$env:USERPROFILE\.claude\hooks\model-tier-guard.py" --selftest     # all cases pass
(Select-String 'agent-safety v' "$env:USERPROFILE\.claude\CLAUDE.md").Count      # expect 2
(Select-String 'fleet-bootstrap v' "$env:USERPROFILE\.claude\CLAUDE.md").Count   # expect 2
(Select-String -SimpleMatch 'Bash(python3 scripts/pr_review.py:*)' "$env:USERPROFILE\.claude\settings.json").Count   # expect 1
(Select-String -SimpleMatch 'Bash(uv run --no-project --script scripts/pr_review.py:*)' "$env:USERPROFILE\.claude\settings.json").Count   # expect 1
```

Live end-to-end (in any repo): attempt a discarded-output write and confirm the Bash tool is blocked:

```sh
gh api graphql -f query='mutation{noop}' -F t="PRRT_x" >/dev/null 2>&1 || true   # blocked by the hook
```

Where the prefix is registered, confirm a command runs contained. The line names a `claude-tool-` scope inside a `claude-tree-` slice, and the second command shows the tree's total ceiling:

```sh
cat /proc/self/cgroup
systemctl --user show -p TasksMax -p MemoryMax "claude-tree-$(echo "$AGENT_CONTAINMENT_ROOT" | tr -d -).slice"
```

## Granting a Write That the Guard Denies

The guard denies a `gh` write whose explicit target sits under an owner other than the checkout's `origin` owner. The denial names `GH_WRITE_GUARD_ALLOW` as the way past it. That grant is the maintainer's to make. Making it is a deliberate act taken outside the session, rather than something an agent does for itself once blocked. This is the one denial a maintainer has to act on, because it is the only one with a grant behind it. The others name a shape to stop using, while this one names a target that may be entirely legitimate.

**The case that raises it is usually a fork.** `origin` is your own fork under your own owner. `upstream` is the project it was forked from under someone else's. Everything aimed at the fork is in scope and never denies. Only the half that leaves the owner stops. That half includes filing an issue on the upstream, opening a pull request against it, and commenting on one there. The grant therefore names the upstream alone, and the fork needs no grant at all. That asymmetry is what a reader hits first, since half the session's writes succeed and the other half do not.

**The grant goes in the checkout's `.claude/settings.local.json`, as an `env` block:**

```json
{
  "env": {
    "GH_WRITE_GUARD_ALLOW": "upstream-owner/upstream-repo second-owner/other-repo third-owner/*"
  }
}
```

**The value is one string holding every grant, never a JSON array**. That is because the hook reads an environment variable and an environment variable is a string. The three tokens above are three separate grants: two naming one repository each, and `third-owner/*` granting every repository under that owner.

Tokens are separated by **any run of whitespace or commas**. So `a/b c/d`, `a/b,c/d`, and `a/b, c/d` all parse to the same two grants and the choice is cosmetic. A token carrying no `/` is ignored, so a malformed entry grants nothing rather than granting everything. A malformed entry also fails silently, which is why the confirmation step below is worth running. Grant the narrowest thing that unblocks the work. That is because a repository grant does not extend to that owner's other repositories, and that containment is the property worth keeping.

**The grant is per checkout, not per host.** `.claude/settings.local.json` lives in the working tree and is git-ignored. So it applies to sessions started in that checkout, and it does not follow the agent into another repository's sessions. That is the intended scope: a grant made to file one upstream issue from one fork does not quietly become a standing permission everywhere.

**Restart the session afterward.** The hook reads the value from the environment the session was launched with. That is what makes the channel one an agent cannot use on itself. It is equally why a grant added to a live session does nothing until that session restarts.

**Two forms look right and leave the write denied.** An inline `GH_WRITE_GUARD_ALLOW=owner/repo gh ...` prefix sets the environment of the `gh` process. An `export` inside a shell call sets the environment of that shell. The hook runs as its own process and sees neither, so the write stays denied with nothing to explain the difference. [`gh-write-guard.py`][write-guard] asserts the inline-prefix case in its own self-test, so this is settled behavior rather than a quirk to work around.

**Confirm the grant loaded before relying on it**. That is because inferring it from a write that no longer denies means learning the answer by making the write. In a restarted session in that checkout, read the variable the hook reads:

```shell
printenv GH_WRITE_GUARD_ALLOW
```

Run it bare, with no `VAR=value` prefix of its own, which would report a value the hook never sees. An empty result means the grant did not load, and the fix is the file location or the restart rather than the token. Feeding the hook a synthetic payload is not a usable probe from inside a session. That is because the payload text carries the very write shape the guard matches, and the guard denies the probe command itself.

Withdraw a grant by deleting the `env` entry and restarting. Nothing expires it, so a grant left in place stays live for every later session in that checkout. That is the reason to remove it once the work that needed it is done.

## Manual settings.json Shape (for Reference)

The installer writes this. It is here so you can inspect it, not as a template to hand-place.
The literal `"python3"` shown for `command` is what the installer writes when a `python3` launcher
is available. It substitutes `sys.executable`'s own absolute path when it is not, most commonly on a
Windows host with no `python3` shim. The `Bash(python3 scripts/pr_review.py:*)` rule in
`permissions.allow` always names `python3` literally, since it matches a command line rather than
the hook's own launcher.
Run the installer rather than hand-copying this block on a host where the two might differ.
The `uv run --no-project --script` rule beside it matches the spelling the fleet's skills print.

```json
{
  "env": {
    "CLAUDE_CODE_SHELL_PREFIX": "<home>/.claude/hooks/tool-containment.py"
  },
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [ { "type": "command", "command": "\"python3\" \"<home>/.claude/hooks/gh-write-guard.py\"" } ] },
      { "matcher": "Agent|Workflow", "hooks": [ { "type": "command", "command": "\"python3\" \"<home>/.claude/hooks/model-tier-guard.py\"" } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "\"python3\" \"<home>/.claude/hooks/stray-process-sweep.py\"", "timeout": 35 } ] }
    ]
  },
  "permissions": {
    "allow": [
      "Bash(python3 scripts/pr_review.py:*)",
      "Bash(uv run --no-project --script scripts/pr_review.py:*)"
    ]
  },
  "autoMode": {
    "environment": [
      "$defaults",
      "[agent-safety] GitHub source control under the GitHub owner of each session checkout's origin remote is trusted.",
      "[agent-safety] The develop and main branches are ruleset-protected. ..."
    ],
    "allow": [
      "$defaults",
      "[agent-safety] Running `scripts/pr_review.py merge` or `scripts/pr_review.py cleanup`, ..."
    ]
  }
}
```

Every other key in the file is left as it stands, `permissions.allow` and the rest of `env` included. The exception is the rules whose prefix this kit owns. An `autoMode` list keeps every entry not carrying this kit's tag, in its own order, and the elided rules are written whole from `AUTO_MODE_RULES`. The `env` entry is written only where the installer reports the prefix set.

## Scope and Limits

- **Per-machine.** `~/.claude/` does not travel, so run the installer on each box. This is the rollout that [#365][issue-365] tracks.
- **Precision over recall for every rule but the branch-bypass one.** The hook denies the specific dangerous shapes where they appear plainly rather than gating every write, so it avoids blocking legitimate work. A shape it does not read is allowed, per the root spec's threat model. Examples are a target held in a run-time word and a write inside a wrapper nested past one level. A shape it does not catch still falls under the behavioral rules.
- **The branch-bypass rule fails closed.** Unlike the write-footgun rules, a push to `main`/`master`/`develop` is denied even when its rules cannot be determined. The API may be unreachable, or the checkout's origin may not resolve to query them. The harm there is a silent success under the maintainer's admin bypass. The rule reads each branch's live rules, so it adapts to every repo (a code-style `develop` denies, a config-style `develop` allows) with no per-repo configuration.
- **Opaque targets are unseen.** The hook cannot see the repository behind a GraphQL node id. That is why rule 2 blocks a *literal* id at all, since a captured `$variable` is trusted. Likewise, the cross-origin check only runs when an `origin` can be resolved and the write names an explicit target the root spec's requirement 3 lists. No owner comparison reads a write whose target is only a node id or a run-time word. The hook reads an inherited `GH_REPO` from its own environment, the one the session was launched with. A value a Bash tool call gets some other way, such as from a shell profile it sources, is not seen. To write elsewhere, override it with a prefix, as in `GH_REPO=<owner>/<repo> gh ...`, or with `-R`.
- **A write or a wait inside a script file is unseen, so never put either in a script to run it.** The hook reads the command the agent runs, which for `bash deploy.sh` is that one string. A `git push` or a `gh` mutation inside the script therefore reaches the server unparsed. An unbounded wait inside that same script is equally unseen, and worse in one way. A denied write fails loudly. A wait the guard never saw runs until something ends it, which is one of the leaks the SessionEnd sweep exists to find afterwards. This is the widest hole in the kit, and an agent opens it by accident. Collecting fifteen repetitive pushes into a loop looks like tidiness, and so does moving a poll into a script to keep a tool call short. Neither looks like disabling a guard. Issue each write and each wait as its own command. A script that only reads, computes, or prepares local commits is fine, because the boundary is the write and the wait rather than the script.
- **Not a credential control.** A fine-grained PAT limited to owned repositories is a separate, stronger structural guard (a hard `403` on any non-owned repo). It is left to per-machine credential setup, out of this kit.
- **Rule 6's ref-checkout and `--ff-only` exemptions are deliberate, not a gap.** The #1073 incident's own literal commands (an unforced `git checkout main`, then `git pull --ff-only`) are exactly this shape. So this rule does not deny the incident's own commands specifically. Denying them would also break the documented base-clone cleanup step in the `repo-worktree` skill, which needs precisely those two. Those commands still carry a concurrent-access hazard: switching HEAD, or fast-forwarding, in a checkout another task might be relying on. That hazard is not decidable from the command text alone, so it stays the prose rule's job. A non-force flag alongside the ref, such as `--detach`, is exempt too, not only a strictly zero-flags invocation. Git's own overwrite-refusal on a dirty tracked file is unaffected by such a flag, verified live.
- **Rule 6's delete-only push exemption is deliberate, not a gap.** A push whose every refspec deletes a remote branch moves no local branch and touches no working tree. So the hazard rule 6 guards is absent. The `cleanup` helper stays the prescribed delete path, since a hand-typed push carries none of its checks. The exemption is narrow. Its remote is a name of letters, digits, `_`, and `-`, never `.` or a path, and it never deletes a protected default. It carries no option beyond delete, dry-run, quiet, and verbose, and no `-c` override. Rule 4 still reads each branch's live rules, so a branch whose rules forbid deletion still denies.
- **Containment runs on Linux and WSL with a systemd user manager delegated the `pids` and `memory` controllers.** It also requires that the deployed prefix runs as its own executable. The manager socket is looked for under `XDG_RUNTIME_DIR` and then under `/run/user/<uid>`, since WSLg points the variable at a directory of its own. A cgroup-v1 or hybrid host starts a scope that enforces neither ceiling, so it is treated as unable to contain. Where the installer left the prefix unset because the host cannot contain, `--report` prints a note saying agent commands run uncontained, and why. The prefix runs through its `env python3` shebang for every hook command, the guard included. That is because Claude Code quotes the whole prefix value as one word, and so no interpreter can be named in front of it. A PreToolUse hook that fails is a non-blocking error, so a prefix that cannot run that way would fail the guard open. The installer and `--report` therefore execute the deployed file directly, as Claude Code does, before treating the host as able to contain. What neither can see is a `python3` on Claude Code's own PATH that differs from the installing shell's. An example is a session an IDE launched under another environment. macOS and Windows have no systemd. A `ulimit -u` fallback caps every process the user owns rather than one command's tree. So it would fail unrelated work rather than stop a runaway. The installer leaves the prefix unset on macOS and Windows, and `--report` flags a prefix registered on a host that can no longer contain. Where the prefix is registered, containment later fails if `systemd-run` goes missing, or the user manager becomes unreachable or loses those two controllers. Each tool call then runs unbounded and prints one `tool-containment:` line on stderr naming which.
- **Only Bash tool calls are scoped.** Claude Code applies `CLAUDE_CODE_SHELL_PREFIX` to hook commands and to stdio MCP server launches as well as to Bash tool calls, verified on 2.1.283. The prefix runs a hook under its shell directly. That is because a PreToolUse hook exiting 1 is a non-blocking error. So a scope that failed to start would let the command the guard denies go ahead. It runs an MCP server directly too, since a server outlives any one session and starts outside one. A tool call is told apart by the Claude Code shell snapshot it sources first or the cwd bookkeeping appended last, either one enough. Where a later Claude Code drops both, tool calls run bare as well, silently, and the sweep's report of surviving processes is what remains. A `claude` session started from a tool call runs its own tool calls in scopes named for itself, each under its own ceiling. That is because sharing the parent's one ceiling would let a heavy build make the kernel kill the nested `claude` process instead. All of them sit in the root session's tree slice. A chain of agents calling agents therefore meets the tree's total ceiling, however few processes each level adds. The tree's memory ceiling is twice a scope's, so one heavy build alone never reaches it. Where several reach it together, the kernel kills the largest process in the tree, usually a build rather than a nested `claude`. A nested session's scopes are stopped when it ends. Where stopping the parent's scope ended the nested session first, the root's sweep stops them as orphans and names the nested session's id. A session that ended with no sweep at all leaves its scopes running, such as one whose terminal was closed. For that case, `python3 ~/.claude/hooks/stray-process-sweep.py --orphans` lists each scope whose recorded agent is no longer running. It groups them by tree and hands over the line that stops them. It stops nothing itself, and never lists a scope whose agent still runs, so its line is safe to run. A scope started by an older prefix records no agent and is listed apart, as unknown. Such a session also leaves its empty `claude-tree-` slice and the ceiling put on it. Both cost nothing and go at the next reboot, or at once with `systemctl --user stop` and `systemctl --user revert` on the slice. Where a tool call's scope fails to start, that call fails with systemd's own error rather than running uncontained. Each contained tool call costs about 35 ms on the reference host, measured as two Python starts and one scope creation. Anything run directly costs about 10 ms. Finding the agent and the tree's slice adds about 4 ms more.
- **The ceilings bound an accident, not an adversary.** A command can raise its own scope's properties with `systemctl --user set-property`. It can also start a scope of its own outside this one, which the session-end stop does not reach. The incident this answers was a mistake rather than an escape, and a ceiling that a deliberate command can lift still stops it.
- **A session's tool-call scopes end with the session.** The sweep stops them whatever they hold, a long build backgrounded on purpose included. That is the one kill in this kit, and it is bounded by the scope name rather than by a judgment about the process. The sweep also stops a scope in the session's tree whose recorded agent is gone, since its own session can no longer stop it. That stop is bounded by the tree's slice and the scope's recorded agent. It never judges what the scope's own processes are. A scope whose agent still runs is left. A scope stop sends SIGTERM and escalates to SIGKILL after two seconds, which keeps it inside the hook's own timeout.
- **The sweep runs on Linux and WSL.** Whether a descendant outlives the agent is decided by comparing session ids, which the sweep reads with `ps -eo sess=,etimes=`. Both are procps format keywords. Windows has no session test at all, and a BSD `ps`, which is what macOS carries, has neither keyword. The script returns immediately on Windows and on macOS rather than printing a failure at every session end there. The installer still deploys and registers it on every platform. That keeps the installer, the payload digest, and the registration checks identical everywhere, which is worth more than a branch nobody has run.
- **The sweep runs at session end, not at subagent end.** `SubagentStop` is where the incident's own loops were born, and it would catch each one hours earlier. Its exit 2 prevents the subagent from stopping, though, so the one channel that reaches a human on `SessionEnd` is not free there. A sweep that cannot report is a sweep that does nothing. The cost of the later moment is real. A run dispatching workers for hours reaches session end once, having finished many rounds, so every leak it wrote is reported together at the end.
- **The sweep's second phase reports and never kills.** Outside a containment scope it cannot tell a leaked wait from a long build backgrounded on purpose. Nothing about ending a process on the maintainer's machine is reversible. It hands over the `kill` line and stops there.
- **The sweep sees the agent's own descendants, and a leak can leave that tree.** A process whose tool-call shell has since exited is reparented to init, so no descendant walk reaches it. What the sweep does see is the backgrounded tool-call shell itself. That is the shape both incidents left behind, and it stays a child of the agent for the whole session. The residual gap is a process that shell in turn backgrounded and abandoned. It is stated here rather than left for a reader to infer from a clean report.
- **Rule 6 needs git 2.31 or newer for `rev-parse --path-format=absolute`.** An older git errors on that flag, and `_is_primary_checkout` reads that as an unresolved target. The rule then fails open silently on that host. There is no crash, no denial, and no diagnostic pointing at the version. `spec/host-tools.json` carries no floor on `git` today (see its own `why` field for the reasoning that applies fleet-wide). This is a narrower, rule-specific dependency worth knowing about rather than a case for raising that floor on its own.

<!-- Repo -->
[spec]: ../README.md
[codex]: ../codex/README.md
[opencode]: ../opencode/README.md
[governance]: ../../../GOVERNANCE.md
[write-guard]: ./gh-write-guard.py
[issue-365]: https://github.com/ptr727/ProjectTemplate/issues/365
[issue-781]: https://github.com/ptr727/ProjectTemplate/issues/781

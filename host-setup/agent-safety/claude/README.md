# Claude Code Write-Safety Kit

This is the Claude Code implementation of the write-safety spec at [`../README.md`][spec] --
per-machine, user-account-scoped guards, deployed first on any system where Claude Code uses the
maintainer's `gh` credentials. Linux, WSL, macOS, and Windows are supported. See the spec for the
requirements this kit satisfies and why each exists. What follows here is Claude-Code-specific
installation and operational detail. Codex and opencode have no equivalent hook yet -- see
[`../codex/README.md`][codex] and [`../opencode/README.md`][opencode] for their status, tracked at
[issue #781][issue-781].

## What It Installs

Into `~/.claude/` (or `%USERPROFILE%\.claude\` on Windows):

- **`hooks/gh-write-guard.py`**: a PreToolUse hook that denies two classes of dangerous action. First, the GitHub **write** footguns behind the cross-repo comment incident: a state-changing `gh` call whose output is discarded, a GraphQL mutation passing a **literal** node id instead of a `$variable`, and a `gh` write whose explicit target is under an owner other than the checkout origin's. Sibling repositories under the same owner are allowed, since the harm this guards is reaching a stranger's repository rather than working across one maintainer's own fleet, and a different owner is allowed only when the maintainer names it in `GH_WRITE_GUARD_ALLOW` (an `owner/repo` list, where `owner/*` grants a whole owner). That variable is read from the environment the session was launched with, which is the one channel an agent cannot set for itself: a hook runs as its own process, so an inline `VAR=x cmd` prefix or an `export` inside a Bash call never reaches it. Second, a **git operation that bypasses a repository protection**. The branch-rule cases (a direct push to a branch that requires a pull request, a force-push where history is protected, a branch delete where deletion is blocked) are judged against the branch's **live** rules, so a code-style `develop` is denied while a config-style `develop` is allowed with no per-repo configuration. A push to a protected-default branch fails closed when its rules cannot be determined, either because the API is unreachable or because the checkout's origin cannot be resolved to query them. The explicit-bypass flags are denied **unconditionally**, since the flag is itself the bypass and needs no branch query: `gh pr merge --admin` overrides the server-side merge gate, and `git commit`/`git push --no-verify` skips the local git hooks. Reads and everything else pass through. It fires even in autonomous / bypass-permissions sessions, which is how the incident happened.
- **Two marker-delimited blocks of host-wide content in `CLAUDE.md`**, loaded into every session on the machine (including ad-hoc work outside any project), installed and updated independently so one can change without rewriting the other.
  - The `agent-safety` block carries restrictions alone, so nothing in it can widen a permission. `Repository Boundaries and Write Safety` mirrors the committed `GOVERNANCE.md` section of the same name, carrying as behavioral guidance its write rules and its rules that a task works in its own checkout and isolates into its own worktree before its first file edit. `Data in Agent-Authored Text` mirrors `GOVERNANCE.md` "Representative Data in Agent-Authored Text", so an agent illustrates with constructed data rather than data observed on the machine. `Authorization Scope and Memory Hygiene` bounds how a granted permission is recorded and read. Each mirrors a rule that otherwise reaches fleet repos only.
  - The `fleet-bootstrap` block carries `Fleet Governance Entry Point`, which names the template repository and routes by the state a repository is actually in. It is separate precisely because it enables rather than restricts, and the safety block's own text says nothing in it widens a permission, so merging the two would contradict that. It is host-wide rather than per repository because the repositories that most need it are the ones carrying no instruction set to point the way, and it mirrors the byte-locked `AGENTS.md` "Fleet Bootstrap" section that a conformant repository carries.
- **The permission rules this kit owns, merged into `settings.json`** beside the hook registration. Each is declared as a prefix and a rule, and a re-run drops every rule the prefix owns before adding the current one, so a rule whose spelling changes updates in place rather than accumulating beside the version it replaced. Ownership requires a rule-syntax delimiter after the prefix, since the prefix ends at the script name and a bare prefix test would also claim a longer path such as `pr_review.py-custom`, so a rule written by hand for a different script is never touched. These widen rather than restrict, which is why they are their own component for the same reason the `fleet-bootstrap` block is separate from the `agent-safety` one. Today the list holds one rule, for `scripts/pr_review.py`, the review loop's reply and resolve. Driving that loop by hand needs a raw GraphQL mutation carrying a node id, which is the shape that reached a stranger's repository, where the script queries the id itself and takes no argument an id fits in. What the rule decides is which command runs without a prompt, and it matches the command text rather than the directory the command runs in, so it reaches a `scripts/pr_review.py` in any checkout that carries one. An absolute path would not narrow that, since the hub is reached as a checkout of the caller's own and its location differs per task, so pinning one path would name a checkout the next task does not use. What bounds it is the rule that an agent reaches the hub as a checkout of its own, fetched immediately before it is read, rather than a copy it happens to find on disk, which the `fleet-bootstrap` block beside this carries and [`GOVERNANCE.md`][governance] "Hub-Hosted Tooling" states in full.

See [`../README.md`][spec] "Requirements" for which of these each rule implements, and "Auditing an
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

Both are thin wrappers around `install.py`, so every OS runs one tested code path. The installer self-tests the hook before registering it, merges the settings.json hook entry and the permission rules without clobbering other keys, and updates each CLAUDE.md block in place by its own markers rather than duplicating it, so the two blocks move independently. The settings file is read once and written once, so the hook and the permission rules land together or not at all.

**Restart Claude Code sessions on the machine afterward** so the new hook and CLAUDE.md load.

## Refreshing After an Upstream Change

The deployed copy on each machine is a snapshot, so when the guard changes upstream (a new rule or a fix) every machine keeps running the old hook until it is refreshed. The installer **is** the refresh: pull the latest template and re-run `install.sh` (or `install.ps1`) on each machine. It re-copies the hook, re-runs the self-test, and re-registers in place, so a re-run is safe and updates the deployed copy. [#365][issue-365] tracks the per-machine rollout and its re-runs.

## Verify (POSIX Shell)

```sh
python3 ~/.claude/hooks/gh-write-guard.py --selftest    # decision matrix: all cases pass
grep -c 'agent-safety v' ~/.claude/CLAUDE.md            # expect 2 (start + end marker)
grep -c 'fleet-bootstrap v' ~/.claude/CLAUDE.md         # expect 2 (start + end marker)
grep -cF 'Bash(python3 scripts/pr_review.py:*)' ~/.claude/settings.json   # expect 1 (never duplicated)
```

On Windows PowerShell:

```powershell
py -3 "$env:USERPROFILE\.claude\hooks\gh-write-guard.py" --selftest    # all cases pass
(Select-String 'agent-safety v' "$env:USERPROFILE\.claude\CLAUDE.md").Count      # expect 2
(Select-String 'fleet-bootstrap v' "$env:USERPROFILE\.claude\CLAUDE.md").Count   # expect 2
(Select-String -SimpleMatch 'Bash(python3 scripts/pr_review.py:*)' "$env:USERPROFILE\.claude\settings.json").Count   # expect 1
```

Live end-to-end (in any repo): attempt a discarded-output write and confirm the Bash tool is blocked:

```sh
gh api graphql -f query='mutation{noop}' -F t="PRRT_x" >/dev/null 2>&1 || true   # blocked by the hook
```

## Granting a Write the Guard Denies

The guard denies a `gh` write whose explicit target sits under an owner other than the checkout's `origin` owner, and the denial names `GH_WRITE_GUARD_ALLOW` as the way past it. That grant is the maintainer's to make, and making it is a deliberate act taken outside the session rather than something an agent does for itself once blocked. This is the one denial a maintainer has to act on, because it is the only one with a grant behind it -- the others name a shape to stop using, while this one names a target that may be entirely legitimate.

**The case that raises it is usually a fork.** `origin` is your own fork under your own owner, and `upstream` is the project it was forked from under someone else's. Everything aimed at the fork is in scope and never denies, and only the half that leaves the owner stops: filing an issue on the upstream, opening a pull request against it, or commenting on one there. The grant therefore names the upstream alone, and the fork needs no grant at all. That asymmetry is what a reader hits first, since half the session's writes succeed and the other half do not.

**The grant goes in the checkout's `.claude/settings.local.json`, as an `env` block:**

```json
{
  "env": {
    "GH_WRITE_GUARD_ALLOW": "upstream-owner/upstream-repo second-owner/other-repo third-owner/*"
  }
}
```

**The value is one string holding every grant, never a JSON array**, since the hook reads an environment variable and an environment variable is a string. The three tokens above are three separate grants: two naming one repository each, and `third-owner/*` granting every repository under that owner.

Tokens are separated by **any run of whitespace or commas**, so `a/b c/d`, `a/b,c/d`, and `a/b, c/d` all parse to the same two grants and the choice is cosmetic. A token carrying no `/` is ignored, so a malformed entry grants nothing rather than granting everything, and it also fails silently, which is why the confirmation step below is worth running. Grant the narrowest thing that unblocks the work, since a repository grant does not extend to that owner's other repositories and that containment is the property worth keeping.

**The grant is per checkout, not per host.** `.claude/settings.local.json` lives in the working tree and is git-ignored, so it applies to sessions started in that checkout and does not follow the agent into another repository's sessions. That is the intended scope: a grant made to file one upstream issue from one fork does not quietly become a standing permission everywhere.

**Restart the session afterward.** The hook reads the value from the environment the session was launched with, which is what makes the channel one an agent cannot use on itself, and it is equally why a grant added to a live session does nothing until that session restarts.

**Two forms look right and leave the write denied.** An inline `GH_WRITE_GUARD_ALLOW=owner/repo gh ...` prefix sets the environment of the `gh` process, and an `export` inside a shell call sets the environment of that shell. The hook runs as its own process and sees neither, so the write stays denied with nothing to explain the difference. [`gh-write-guard.py`][write-guard] asserts the inline-prefix case in its own self-test, so this is settled behavior rather than a quirk to work around.

**Confirm the grant loaded before relying on it**, since inferring it from a write that no longer denies means learning the answer by making the write. In a restarted session in that checkout, read the variable the hook reads:

```shell
printenv GH_WRITE_GUARD_ALLOW
```

Run it bare, with no `VAR=value` prefix of its own, which would report a value the hook never sees. An empty result means the grant did not load, and the fix is the file location or the restart rather than the token. Feeding the hook a synthetic payload is not a usable probe from inside a session, because the payload text carries the very write shape the guard matches and the guard denies the probe command itself.

Withdraw a grant by deleting the `env` entry and restarting. Nothing expires it, so a grant left in place stays live for every later session in that checkout, which is the reason to remove it once the work that needed it is done.

## Manual settings.json Shape (for Reference)

The installer writes this. It is here so you can inspect or hand-place it:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [ { "type": "command", "command": "\"python3\" \"<home>/.claude/hooks/gh-write-guard.py\"" } ] }
    ]
  },
  "permissions": {
    "allow": [ "Bash(python3 scripts/pr_review.py:*)" ]
  }
}
```

Every other key in the file is left as it stands, `permissions.allow` included, apart from the rules whose prefix this kit owns.

## Scope and Limits

- **Per-machine.** `~/.claude/` does not travel, so run the installer on each box. This is the rollout that [#365][issue-365] tracks.
- **Precision over recall for the write footguns.** The hook denies the specific dangerous write shapes with high confidence rather than gating every write, so it never blocks legitimate work. A shape it does not catch still falls under the behavioral rules.
- **The branch-bypass rule fails closed.** Unlike the write-footgun rules, a push to `main`/`master`/`develop` is denied even when its rules cannot be determined (the API is unreachable, or the checkout's origin cannot be resolved to query them), because the harm there is a silent success under the maintainer's admin bypass. The rule reads each branch's live rules, so it adapts to every repo (a code-style `develop` denies, a config-style `develop` allows) with no per-repo configuration, and hands the exact command to the maintainer to run when a bypass is genuinely intended.
- **Opaque targets are unseen.** The hook cannot see the repository behind a GraphQL node id, which is exactly why rule 2 blocks a *literal* id at all, since a captured `$variable` is trusted. Likewise, the cross-origin check only runs when an `origin` can be resolved and the write names an explicit `-R`/`repos/<owner>/<repo>` target. A write from a non-git directory, or one whose target is only a node id, is evaluated by rules 1 and 2 alone.
- **A write inside a script file is unseen, so never batch writes into a script.** The hook reads the command the agent runs, which for `bash deploy.sh` is that one string, so a `git push` or a `gh` mutation inside the script reaches the server without the hook ever parsing it. This is the widest hole in the kit and it is one an agent opens by accident, since collecting fifteen repetitive pushes into a loop looks like tidiness rather than like disabling a guard. Issue each write as its own command. A script that only reads, computes, or prepares local commits is fine, because the boundary is the write and not the script.
- **The hook's own parser over-blocks a `git push` followed by a newline.** Git and GitHub are not involved in this one: the hook splits the command string to find each `git push` and its arguments, and that split ends an argument list at `&&` but not at a newline, so it reads every token on a later line of the same command as a refspec of that push. Measured against the installed hook, `git push -u origin revendor/x` resolves to that one branch, while the same push followed by a newline and a `gh pr create` naming `develop` as its base resolves to five, meaning `revendor/x`, `gh`, `pr`, `create`, and `develop`. The hook then denies the push as a direct push to a protected branch that the push never named. The direction is safe, since it blocks rather than admits, but the denial names a bypass the agent never attempted, and a guard that cries wolf is one an agent starts working around. Until the parser is fixed, issue the push as its own command, which is the rule directly above in any case. Tracked in `TODO.md`.
- **Not a credential control.** A fine-grained PAT limited to owned repositories is a separate, stronger structural guard (a hard `403` on any non-owned repo) and is left to per-machine credential setup, out of this kit.

<!-- Repo -->
[spec]: ../README.md
[codex]: ../codex/README.md
[opencode]: ../opencode/README.md
[governance]: ../../../GOVERNANCE.md
[write-guard]: ./gh-write-guard.py
[issue-365]: https://github.com/ptr727/ProjectTemplate/issues/365
[issue-781]: https://github.com/ptr727/ProjectTemplate/issues/781

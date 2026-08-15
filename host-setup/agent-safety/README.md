# Agent Write-Safety Kit

Per-machine, user-account-scoped guards against an agent making a mis-targeted GitHub **write** under the maintainer's identity, or a **git operation that bypasses a branch rule or overrides a required check**, meaning a push, force-push, or delete that an active branch rule forbids, or an override flag (`--admin` past the server-side merge gate, `--no-verify` past the local git hooks). Deploy it as the **first thing on any new system** where Claude Code runs with the `gh` credentials logged in (Linux, WSL, macOS, Windows).

## What It Installs

Into `~/.claude/` (or `%USERPROFILE%\.claude\` on Windows):

- **`hooks/gh-write-guard.py`**: a PreToolUse hook that denies two classes of dangerous action. First, the GitHub **write** footguns behind the cross-repo comment incident: a state-changing `gh` call whose output is discarded, a GraphQL mutation passing a **literal** node id instead of a `$variable`, and a `gh` write whose explicit target is under an owner other than the checkout origin's. Sibling repositories under the same owner are allowed, since the harm this guards is reaching a stranger's repository rather than working across one maintainer's own fleet, and a different owner is allowed only when the maintainer names it in `GH_WRITE_GUARD_ALLOW` (an `owner/repo` list, where `owner/*` grants a whole owner). That variable is read from the environment the session was launched with, which is the one channel an agent cannot set for itself: a hook runs as its own process, so an inline `VAR=x cmd` prefix or an `export` inside a Bash call never reaches it. Second, a **git operation that bypasses a repository protection**. The branch-rule cases (a direct push to a branch that requires a pull request, a force-push where history is protected, a branch delete where deletion is blocked) are judged against the branch's **live** rules, so a code-style `develop` is denied while a config-style `develop` is allowed with no per-repo configuration. A push to a protected-default branch fails closed when its rules cannot be determined, either because the API is unreachable or because the checkout's origin cannot be resolved to query them. The explicit-bypass flags are denied **unconditionally**, since the flag is itself the bypass and needs no branch query: `gh pr merge --admin` overrides the server-side merge gate, and `git commit`/`git push --no-verify` skips the local git hooks. Reads and everything else pass through. It fires even in autonomous / bypass-permissions sessions, which is how the incident happened.
- **Two marker-delimited blocks of host-wide content in `CLAUDE.md`**, loaded into every session on the machine (including ad-hoc work outside any project), installed and updated independently so one can change without rewriting the other.
  - The `agent-safety` block carries restrictions alone, so nothing in it can widen a permission. `Repository Boundaries and Write Safety` mirrors the committed `GOVERNANCE.md` section of the same name, carrying as behavioral guidance its write rules and its rules that a task works in its own checkout and isolates into its own worktree before its first file edit. `Data in Agent-Authored Text` mirrors `GOVERNANCE.md` "Representative Data in Agent-Authored Text", so an agent illustrates with constructed data rather than data observed on the machine. `Authorization Scope and Memory Hygiene` bounds how a granted permission is recorded and read. Each mirrors a rule that otherwise reaches fleet repos only.
  - The `fleet-bootstrap` block carries `Fleet Governance Entry Point`, which names the template repository and routes by the state a repository is actually in. It is separate precisely because it enables rather than restricts, and the safety block's own text says nothing in it widens a permission, so merging the two would contradict that. It is host-wide rather than per repository because the repositories that most need it are the ones carrying no instruction set to point the way, and it mirrors the byte-locked `AGENTS.md` "Fleet Bootstrap" section that a conformant repository carries.
- **The permission rules this kit owns, merged into `settings.json`** beside the hook registration. Each is declared as a prefix and a rule, and a re-run drops every rule the prefix owns before adding the current one, so a rule whose spelling changes updates in place rather than accumulating beside the version it replaced. Ownership requires a rule-syntax delimiter after the prefix, since the prefix ends at the script name and a bare prefix test would also claim a longer path such as `pr_review.py-custom`, so a rule written by hand for a different script is never touched. These widen rather than restrict, which is why they are their own component for the same reason the `fleet-bootstrap` block is separate from the `agent-safety` one. Today the list holds one rule, for `scripts/pr_review.py`, the review loop's reply and resolve. Driving that loop by hand needs a raw GraphQL mutation carrying a node id, which is the shape that reached a stranger's repository, where the script queries the id itself and takes no argument an id fits in. What the rule decides is which command runs without a prompt, and it matches the command text rather than the directory the command runs in, so it reaches a `scripts/pr_review.py` in any checkout that carries one. An absolute path would not narrow that, since the hub is reached as a checkout of the caller's own and its location differs per task, so pinning one path would name a checkout the next task does not use. What bounds it is the rule that an agent reaches the hub as a checkout of its own, fetched immediately before it is read, rather than a copy it happens to find on disk, which the `fleet-bootstrap` block beside this carries and [`GOVERNANCE.md`][governance] "Hub-Hosted Tooling" states in full.

The hook is the mechanical backstop. The CLAUDE.md rules and the carried GOVERNANCE.md rules are the behavioral layer. Prose alone is not enough, since the incident happened under prose rules, so both ship. The GitHub write rules have a hook behind them, as do the git operations that bypass a repository protection, while which checkout a command belongs in, the data a line of text quotes, and how an authorization is recorded are prose only, since no hook can see any of the three.

## Install (Idempotent, Safe to Re-Run to Update)

```sh
# Linux / WSL / macOS
host-setup/agent-safety/install.sh
```

```powershell
# Windows - the .\ prefix is required, PowerShell does not run a script from a relative path without it
.\host-setup\agent-safety\install.ps1
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

## Granting a Cross-Owner Write

The cross-origin rule is the one denial a maintainer has to act on, because it is the only one with a grant behind it. The others name a shape to stop using, while this one names a target that may be entirely legitimate.

`GH_WRITE_GUARD_ALLOW` is one string holding every grant, since it is an environment variable, and it is split into `owner/repo` tokens on any run of whitespace or commas, so `a/b c/d` and `a/b, c/d` are the same two grants. `owner/*` grants a whole owner. A token carrying no `/` is ignored, so a malformed grant grants nothing, and a repository grant does not extend to that owner's other repositories.

The hook reads it from the environment the session was launched with, which is the one channel an agent cannot use on itself: the hook runs as its own process, so an inline `VAR=x cmd` prefix and an `export` inside a Bash call both leave the write denied. Granting is therefore a deliberate act taken outside the session, and a blocked agent asks rather than unblocks itself.

The channel that works is an `env` block in the checkout's `.claude/settings.local.json`, which scopes the grant to sessions started in that checkout, followed by a session restart. The worked example, the fork case that raises this most often, and how to confirm a grant loaded without making the write are in [`docs/host-setup.md` "Granting a Write the Guard Denies"][host-setup-grant].

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
[governance]: ../../GOVERNANCE.md
[host-setup-grant]: ../../docs/host-setup.md#granting-a-write-the-guard-denies
[issue-365]: https://github.com/ptr727/ProjectTemplate/issues/365

# Agent Write-Safety Kit

Per-machine, user-account-scoped guards against an agent making a mis-targeted GitHub **write** under the maintainer's identity, or a **git operation that bypasses a branch rule or overrides a required check** - a push, force-push, or delete that an active branch rule forbids, or an override flag (`--admin` past the server-side merge gate, `--no-verify` past the local git hooks). Deploy it as the **first thing on any new system** where Claude Code runs with the `gh` credentials logged in (WSL, Linux, macOS, Proxmox, Windows).

## What It Installs

Into `~/.claude/` (or `%USERPROFILE%\.claude\` on Windows):

- **`hooks/gh-write-guard.py`** - a PreToolUse hook that denies two classes of dangerous action. First, the GitHub **write** footguns behind the cross-repo comment incident: a state-changing `gh` call whose output is discarded, a GraphQL mutation passing a **literal** node id instead of a `$variable`, and a `gh` write whose explicit target is outside the checkout's `origin`. Second, a **git operation that bypasses a repository protection**. The branch-rule cases - a direct push to a branch that requires a pull request, a force-push where history is protected, a branch delete where deletion is blocked - are judged against the branch's **live** rules, so a code-style `develop` is denied while a config-style `develop` is allowed with no per-repo configuration. A push to a protected-default branch fails closed when its rules cannot be determined - the API is unreachable, or the checkout's origin cannot be resolved to query them. The explicit-bypass flags are denied **unconditionally**, since the flag is itself the bypass and needs no branch query: `gh pr merge --admin` overrides the server-side merge gate, and `git commit`/`git push --no-verify` skips the local git hooks. Reads and everything else pass through. It fires even in autonomous / bypass-permissions sessions, which is how the incident happened.
- **A `## GitHub Write Safety (Any Project, Every Session)` section in `CLAUDE.md`** - the same three rules as behavioral guidance, loaded into every session on the machine (including ad-hoc work outside any project). It mirrors the committed `AGENTS.md` "Repository Boundaries and Write Safety" rules, which only reach fleet repos.

The hook is the mechanical backstop. The CLAUDE.md rules and the carried AGENTS.md rules are the behavioral layer. Prose alone is not enough - the incident happened under prose rules - so both ship.

## Install (Idempotent - Safe to Re-Run to Update)

```sh
# Linux / WSL / macOS / Proxmox
host-setup/agent-safety/install.sh
```

```powershell
# Windows
host-setup\agent-safety\install.ps1
```

Both are thin wrappers around `install.py`, so every OS runs one tested code path. The installer self-tests the hook before registering it, merges the settings.json entry without clobbering other keys, and updates the CLAUDE.md block in place (marker-delimited) rather than duplicating it.

**Restart Claude Code sessions on the machine afterward** so the new hook and CLAUDE.md load.

## Refreshing After an Upstream Change

The deployed copy on each machine is a snapshot, so when the guard changes upstream (a new rule or a fix) every machine keeps running the old hook until it is refreshed. The installer **is** the refresh: pull the latest template and re-run `install.sh` (or `install.ps1`) on each machine. It re-copies the hook, re-runs the self-test, and re-registers in place, so a re-run is safe and updates the deployed copy. [#365][issue-365] tracks the per-machine rollout and its re-runs.

## Verify (POSIX Shell)

```sh
python3 ~/.claude/hooks/gh-write-guard.py --selftest    # decision matrix: all cases pass
grep -c 'agent-safety v' ~/.claude/CLAUDE.md            # expect 2 (start + end marker)
```

On Windows PowerShell:

```powershell
py -3 "$env:USERPROFILE\.claude\hooks\gh-write-guard.py" --selftest    # all cases pass
(Select-String 'agent-safety v' "$env:USERPROFILE\.claude\CLAUDE.md").Count   # expect 2
```

Live end-to-end (in any repo): attempt a discarded-output write and confirm the Bash tool is blocked:

```sh
gh api graphql -f query='mutation{noop}' -F t="PRRT_x" >/dev/null 2>&1 || true   # blocked by the hook
```

## Manual settings.json Shape (for Reference)

The installer writes this. It is here so you can inspect or hand-place it:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [ { "type": "command", "command": "\"python3\" \"<home>/.claude/hooks/gh-write-guard.py\"" } ] }
    ]
  }
}
```

## Scope and Limits

- **Per-machine.** `~/.claude/` does not travel, so run the installer on each box. This is the rollout that [#365][issue-365] tracks.
- **Precision over recall for the write footguns.** The hook denies the specific dangerous write shapes with high confidence rather than gating every write, so it never blocks legitimate work. A shape it does not catch still falls under the behavioral rules.
- **The branch-bypass rule fails closed.** Unlike the write-footgun rules, a push to `main`/`master`/`develop` is denied even when its rules cannot be determined (the API is unreachable, or the checkout's origin cannot be resolved to query them), because the harm there is a silent success under the maintainer's admin bypass. The rule reads each branch's live rules, so it adapts to every repo (a code-style `develop` denies, a config-style `develop` allows) with no per-repo configuration, and hands the exact command to the maintainer to run when a bypass is genuinely intended.
- **Opaque targets are unseen.** The hook cannot see the repository behind a GraphQL node id, which is exactly why rule 2 blocks a *literal* id at all - a captured `$variable` is trusted. Likewise, the cross-origin check only runs when an `origin` can be resolved and the write names an explicit `-R`/`repos/<owner>/<repo>` target. A write from a non-git directory, or one whose target is only a node id, is evaluated by rules 1 and 2 alone.
- **Not a credential control.** A fine-grained PAT limited to owned repositories is a separate, stronger structural guard (a hard `403` on any non-owned repo) and is left to per-machine credential setup, out of this kit.

<!-- Repo -->
[issue-365]: https://github.com/ptr727/ProjectTemplate/issues/365

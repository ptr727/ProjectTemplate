# Instructions for AI Coding Agents

**ProjectTemplate** is a polyglot template repo. The .NET side ships under [`NuGetLibrary/`](./NuGetLibrary/) (plus `Console/`, `Tests/`, `Benchmarks/`, `CodeGen/`); the Python side ships under [`PyPiLibrary/`](./PyPiLibrary/). This file is the single source of truth for cross-cutting rules. Language-specific style guides live next to the code:

- .NET — [`CODESTYLE.md`](./CODESTYLE.md)
- Python — [`PyPiLibrary/CODESTYLE.md`](./PyPiLibrary/CODESTYLE.md)

Treat this file as authoritative for everything else; don't restate its rules elsewhere.

## Git and Commit Rules

**These rules are absolute — no exceptions:**

- **Never make git commits.** AI coding agents cannot produce cryptographically signed commits. All commits must be signed (SSH/GPG) and must be made by the developer. Stage changes with `git add` and leave the commit to the developer.
- **Never force push.** Do not run `git push --force` or `git push --force-with-lease` under any circumstances. Force pushing rewrites shared history and can cause data loss.
- **Never run destructive git commands** (`git reset --hard`, `git checkout .`, `git restore .`, `git clean -f`) without explicit developer instruction.
- **Staging is the limit.** Prepare and stage file changes; the developer runs `git commit` in their own environment where signing keys are available.

## Branching Model

- `develop` is the integration branch. Feature branches → `develop` is **squash-only**; develop is kept linear.
- `develop` → `main` is **merge-commit only** (no squash, no rebase). Merge commits preserve develop's commit list as a real second-parent reference on main, which is what makes the "release on every push" model attribute releases to the develop commits that produced them. Branch protection enforces this: the develop ruleset allows only `squash`, the main ruleset allows only `merge`.
- All commits on both branches must be cryptographically signed (SSH or GPG). Squash and merge commits created via the GitHub UI are signed by GitHub's web-flow key.
- **`develop` is forward-only — no `main → develop` back-merges.** The develop ruleset's squash-only setting physically blocks merge commits on develop. Historical back-merge commits visible in `git log` (`b9b0447`, `410ba56`, `ffb9e64`, `5ce95cf`, etc.) predate this rule and must not be repeated.
- **Bots (Dependabot and codegen) target both `main` and `develop` in parallel.** [`.github/dependabot.yml`](./.github/dependabot.yml) duplicates every ecosystem entry (one per branch) and [`.github/workflows/run-codegen-pull-request-task.yml`](./.github/workflows/run-codegen-pull-request-task.yml) runs as a matrix over both branches with branch names `codegen-main` and `codegen-develop`. Each branch absorbs its own bot PRs independently, so neither falls behind, and the forward-only rule still holds (nothing is back-merged from main to develop — both branches receive their updates directly). The merge-bot ([`.github/workflows/merge-bot-pull-request.yml`](./.github/workflows/merge-bot-pull-request.yml)) dispatches `--squash` or `--merge` from each PR's base ref via a `case` statement so the form matches the ruleset on either base. Dependabot **security** PRs (CVE-driven) always open against the repo default branch (`main`) regardless of `target-branch` — the same `case` statement covers them.
- **Maintainer-pushed commits on a bot PR auto-disable auto-merge.** The merge-bot's `merge-dependabot` and `merge-codegen` jobs only fire on `opened` / `reopened` events (auto-merge is enabled exactly once per PR). When a maintainer pushes commits to a bot's branch (a `synchronize` event with an actor that isn't the same bot), the merge-bot's `disable-auto-merge-on-maintainer-push` job fires and calls `gh pr merge --disable-auto`. The maintainer's commits stay in the PR but won't auto-merge with the bot's content; re-enable auto-merge manually (`gh pr merge --auto <PR>` or the GitHub UI) when ready.
- **Why parallel dual-target rather than develop-only with eventual flow-through:** push-distribution channels (HACS for Home Assistant integrations, Linux distros that vendor from `main`, etc.) consume `main` directly. A develop-only model would leave `main` running stale code during long-running develop features. Codegen content can also be production-critical (live API-derived data, language lists, build catalogs) rather than just sample/demo content, so both branches need fresh codegen on their own cadence.

## Pull Request Title and Commit Message Conventions

### Format

- Imperative subject summarizing the change, ≤72 characters, no trailing period. ("Add 24-hour PM2.5 average sensor", not "Added X" or "Adds X".)
- Optional body, blank-line separated, explaining *why* the change is being made when that's non-obvious. The diff shows *what*.

### Rules

- Don't write `update stuff`, `wip`, or other vague titles. (Dependabot's default `Bump X from Y to Z` titles are fine — keep them.)
- Don't add `Co-Authored-By:` lines unless the developer explicitly asks.
- Don't put release-bump magnitude in the title — no "minor", "patch", "release v0.2.0", etc. Nerdbank.GitVersioning computes the next release version from `version.json` + git history. Dependency versions in dependency-bump titles are fine and expected.
- Use US English spelling and match the existing heading style of the file you're editing: title case with lowercase short bind words (a, an, the, and, but, or, of, in, on, at, to, by, for, from); hyphenated compounds capitalize both parts unless the second is a short preposition (*Built-in*, *EPA-Corrected*, *24-Hour*).

### Examples

```text
Add structured logging extensions to library
Pin softprops/action-gh-release to commit SHA
Drop net8.0 multi-targeting from console project
Bump xunit.v3 from 3.2.2 to 3.3.0
Clarify devcontainer setup steps in README
```

## Documentation Style Conventions

### Markdown

- Use reference-style links for any URL referenced more than once or appearing in lists; alphabetize the reference definitions block.
- Inline single-use relative links (e.g. `[CODESTYLE.md](./CODESTYLE.md)`) are fine.
- One logical paragraph per line; no hard-wrap line-length limit.
- Headings follow the title-case-with-short-bind-words rule from the PR-title section.

### Quantitative Claims

- Any quantitative claim in `README.md` (counts, sizes, version floors, supported platforms) must be verified against current code. If a doc number is derived from a code constant, mark the dependency in a source-code comment so the next editor knows to update both.

## PR Review Etiquette

The repo runs a review loop on every PR: local agent iteration plus remote automated review (GitHub Copilot is the configured reviewer). Treat this as a contract regardless of which local agent authored the changes.

### Expected Review Loop

1. Push changes to the PR branch.
2. Confirm a review was requested for the **current head SHA** (auto-trigger is unreliable; request explicitly).
3. Wait for review activity on that head.
4. Triage findings.
5. Apply fixes or write a rationale for declines.
6. Reply to each thread and resolve what was addressed.
7. Re-run the loop after every fix push until no actionable findings remain.

`mergeStateStatus: CLEAN` only checks required statuses; it does not block on bot review comments. Merge only after review on the latest head SHA is confirmed and actionable findings are closed.

For provider-specific mechanics (how to request review, query review state, post replies, resolve threads), see the **GitHub Copilot Review Runbook** in [.github/copilot-instructions.md](./.github/copilot-instructions.md). This file owns the contract; that file owns the mechanics.

### Triaging Review Comments

For each comment, classify before responding:

- **Bug** — wrong behavior, missing test coverage, or a real divergence between code and docs. Fix it. Reply with the fixing commit SHA when done.
- **Style/convention** — the comment cites a rule from this file or a language-specific style guide. Two cases:
  - The cited rule matches what the existing codebase already does → fix the offending code.
  - The cited rule contradicts what's in the tree, or industry norm → **update the rule instead of the code**. The rule is wrong, not the code. Bouncing the same code across rounds is the symptom of a wrong rule. Heuristic: three rounds on the same style category means the rule needs adjusting and the user should authorize the rule change.
- **Architectural opinion** — the comment proposes a different design ("constrain this to disabled-by-default", "move it elsewhere", "add a runtime guardrail"). This is judgement, not a bug. Surface it to the user with a recommendation; don't apply unilaterally.

### Responding and Resolution Expectations

Reply inline with either the fixing commit SHA (for accepted issues) or a concise rationale (for declines). Resolve review threads when addressed or intentionally declined with rationale. Issue-level comments (those at `repos/.../issues/<N>/comments` rather than tied to a specific line) have no resolution action — acknowledge with a reply if needed and move on.

After the final push on a PR, sweep older threads from earlier rounds whose code paths no longer exist; otherwise stale unresolved markers remain in the review UI.

### Escalating to the User

Bring the user in when:

- **Genuine design trade-off** surfaces (fail-open vs fail-closed, narrow vs broad refactor scope, "should we add a guardrail or trust the docstring"). Triage, recommend, ask.
- **Repeated friction** across rounds without convergence — that's the rule-needs-updating signal. Stop, summarize the pattern, and let the user authorize the rule change.
- **Architectural redesign** is requested rather than a bug fix. Surface with a recommendation; never apply unilaterally.

Anti-pattern: don't keep flipping the code on the same style point. Flip the rule once and stick to the rule.

## Workflow YAML Conventions

These conventions describe the target state. New and modified workflows must respect them; the rest of the repo is expected to be brought up to the same standard. Sweep PRs that apply a rule everywhere are welcome when a rule changes.

- **Action pinning**: pin **every** action — first-party (`actions/*`) and third-party — to a commit SHA with a trailing `# vX.Y.Z` comment, so Renovate / Dependabot can still bump it but a tag swap can't change the executed code. Documented exception: [`dotnet/nbgv`](./.github/workflows/get-version-task.yml) is consumed via `@master` because the upstream tag stream lags `master` substantially and Dependabot's tag-tracking would propose a downgrade — the rationale is documented inline in that workflow.
- **Filename**: reusable workflows (those with `on: workflow_call`) end in `-task.yml`. Entry-point workflows (`on: push` / `pull_request` / `schedule` / `workflow_dispatch`) do NOT use the `-task` suffix; they end with what they do — `-pull-request.yml`, `-release.yml`, etc. The suffix carries semantic meaning: a `-task.yml` file is meant to be `uses:`-d, never triggered directly.
- **Workflow `name:`** (the top-level `name:` field): reusable workflow names end in **"task"** (e.g. `Build PyPI library task`); entry-point workflow names end in **"action"** (e.g. `Publish project release action`, `Test pull request action`). The displayed action name in the GitHub Actions UI tells you at a glance whether you're looking at an orchestrator or a callee.
- **Job and step `name:` suffixes**: every job's `name:` ends in **"job"**; every step's `name:` ends in **"step"**. **Exception**: a job whose `name:` is also referenced as a required-status-check `context:` in a branch ruleset (currently `Check pull request workflow status` in `test-pull-request.yml`) keeps the ruleset-bound name verbatim — renaming would silently break required-status-check enforcement. Do not "fix" that name; if a future job becomes ruleset-bound, mark it the same way.
- **Concurrency**: top-level workflows declare `concurrency: { group: '${{ github.workflow }}-${{ github.ref }}', cancel-in-progress: true }` so a fresh push supersedes an in-flight run on the same ref.
- **Shells**: multi-line `run:` blocks with bash start with `set -euo pipefail` — fail fast, fail on undefined vars, fail on a failed pipe segment.
- **Conditionals**: multi-line `if:` uses folded scalar `if: >-` so YAML preserves whitespace correctly. Literal block (`if: |`) is wrong because it embeds newlines inside the boolean expression.
- **Boolean inputs**: workflows triggered both via `workflow_call` and `workflow_dispatch` must declare each boolean input in *both* trigger blocks — one definition does not propagate to the other. `workflow_call` delivers booleans as actual booleans; `workflow_dispatch` delivers them as the *strings* `"true"`/`"false"`. Any `if:` consuming a boolean input must compare against both forms — `if: ${{ inputs.foo == true || inputs.foo == 'true' }}`.
- **Reusable workflows**: job-level `permissions:` are validated *before* the `if:` evaluates, so even a skipped job needs valid permissions declared. A `release` job with `permissions: contents: write` and `if: ${{ inputs.publish }}` will still cause `startup_failure` on a caller that doesn't grant `contents: write`. Either declare permissions at the call site, or omit the inner block and inherit.
- **Allowlist `success` and `skipped` explicitly** when chaining jobs across optional dependencies — `!= 'failure'` lets `cancelled` through (timeout, runner failure, manual cancel). Use `(needs.X.result == 'success' || needs.X.result == 'skipped')`.
- **Tag pinning on releases**: when using `softprops/action-gh-release` (or any tag-creating action), pass `target_commitish: ${{ github.sha }}` explicitly. Without it, GitHub's REST API defaults the new tag to the repository's default branch instead of the commit that built the artifact.

## Devcontainer

The repo ships **two per-language devcontainers** so each container carries only one toolchain (and the matching VS Code extensions): [`.devcontainer/dotnet/devcontainer.json`](./.devcontainer/dotnet/devcontainer.json) (.NET 10 SDK) and [`.devcontainer/python/devcontainer.json`](./.devcontainer/python/devcontainer.json) (Python 3.14 + version-pinned `uv`). Open [`DotNet.code-workspace`](./DotNet.code-workspace) or [`Python.code-workspace`](./Python.code-workspace) and pick **Reopen in Container** to land in the matching one.

Both containers bind-mount the host SSH signing key's *public half* (`~/.ssh/id_ed25519.pub`), `~/.config/git/allowed_signers`, and `~/.config/gh` so commits inside the container are SSH-signed (signing happens via the forwarded `ssh-agent` socket — the private key never enters the container) and, *when the host's `gh` token is file-backed*, `gh` is pre-authenticated. On Keychain (macOS) or libsecret (Linux) hosts, `~/.config/gh/hosts.yml` carries no `oauth_token`, so container `gh` is unauthenticated until the contributor opts into `gh auth login` inside the container. See [docs/devcontainer.md](./docs/devcontainer.md) for full setup, [docs/host-setup.md](./docs/host-setup.md) for prerequisites, and [docs/ssh-signing.md](./docs/ssh-signing.md) for the SSH commit signing details.

Each devcontainer's `customizations.vscode.extensions` mirrors the `recommendations` array in its matching workspace file — when you add an extension to one, add it to the other.

## Project Structure (Languages)

- **.NET projects** (build with `dotnet build`, test with `dotnet test`):
  - `NuGetLibrary/` — core reusable .NET NuGet library (published as `ptr727.ProjectTemplate.Library`)
  - `Console/` — CLI app using System.CommandLine
  - `Tests/` — xUnit + AwesomeAssertions
  - `Benchmarks/` — BenchmarkDotNet
  - `CodeGen/` — internal codegen tooling
  - **Style guide: [`CODESTYLE.md`](./CODESTYLE.md)**.
- **Python project** (env/build/test with `uv` from inside `PyPiLibrary/`):
  - `PyPiLibrary/` — PyPI library template, published as `ptr727-projecttemplate-library`
  - **Style guide: [`PyPiLibrary/CODESTYLE.md`](./PyPiLibrary/CODESTYLE.md)**.
- **Cross-cutting**:
  - `.github/` — workflows, Dependabot, Copilot instructions
  - `.devcontainer/dotnet/` and `.devcontainer/python/` — per-language devcontainer configs + post-create scripts
  - `DotNet.code-workspace`, `Python.code-workspace` — per-language VS Code workspace files (each pairs with its devcontainer)
  - `.vscode/` — debug configs and tasks (.NET-oriented)
  - `Docker/` — multi-platform Linux container build for the Console app

When you touch code in either language, also respect that language's style guide. Conventions in this file (PR titles, branching, US English, devcontainer behavior, workflow YAML) apply uniformly to both languages.

## Quick Start for Derived Projects

1. **Clone this template** as the baseline for your project.
2. **Decide** which language sides you need. If you need only one, delete the other folder and its references — see the relevant CODESTYLE for the deletion checklist.
3. **Read** [CODESTYLE.md](./CODESTYLE.md) (.NET) and/or [PyPiLibrary/CODESTYLE.md](./PyPiLibrary/CODESTYLE.md) (Python) for the per-language style.
4. **Update project-specific values** — `PackageId`/`RootNamespace` in `.csproj`, `name` in `pyproject.toml`, namespace conventions, `README.md`, `HISTORY.md`, `version.json`, `LICENSE`, NuGet/PyPI badge URLs.
5. **Run tools before first commit**:
   - .NET: `dotnet tool restore`.
   - Python: `cd PyPiLibrary && uv sync`.
   - Optional pre-commit hooks (off by default) — see README "Optional: enable git hooks locally".
6. **Wire up release credentials** when ready to publish — see the README's release notes section and [PyPiLibrary/README.md](./PyPiLibrary/README.md) for PyPI Trusted Publisher setup.

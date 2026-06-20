# Instructions for AI Coding Agents

**ProjectTemplate** is a polyglot template repo. The .NET side ships under [`NuGetLibrary/`](./NuGetLibrary/) (plus `Console/`, `Tests/`, `Benchmarks/`, `CodeGen/`); the Python side ships under [`PyPiLibrary/`](./PyPiLibrary/). This file is the single source of truth for cross-cutting rules. Language-specific style guides live next to the code:

- .NET - [`CODESTYLE.md`](./CODESTYLE.md)
- Python - [`PyPiLibrary/CODESTYLE.md`](./PyPiLibrary/CODESTYLE.md)

Treat this file as authoritative for everything else; don't restate its rules elsewhere.

## Git and Commit Rules

- **Default to staging, not committing.** Stage changes with `git add` and leave `git commit` to the developer unless the developer has explicitly authorized the agent to commit for the current ask ("commit this", "open a PR", etc.). Authorization is scope-bound - it covers the commits needed for that specific task, not a blanket commit license for the rest of the session.
- **All commits must be cryptographically signed (SSH or GPG).** Branch protection enforces this on both branches; unsigned commits are rejected on push. Signing depends on environment configuration - `git config commit.gpgsign true`, a configured `user.signingkey`, and a working signing agent (loaded `ssh-agent` for SSH, or `gpg-agent` for GPG). If signing is not configured in the environment, **do not commit** - surface the missing config to the developer and stop at `git add`. Verify before any agent-authored commit (`git config --get commit.gpgsign && ssh-add -L` or the GPG equivalent). **Signing must be live before the *first* commit, not retrofitted.** Turning on `Require signed commits` against a branch that already has unsigned commits forces a rewrite of that entire history to re-sign it - changing every commit SHA and making whoever does the rewrite the committer and signer of every commit (a rebase preserves the `author` field but not the original signatures; you cannot sign another contributor's commits for them). During new-repo setup, never create commits until signing is verified.
- **Never force push.** Do not run `git push --force` or `git push --force-with-lease` under any circumstances. Force pushing rewrites shared history and can cause data loss.
- **Never run destructive git commands** (`git reset --hard`, `git checkout .`, `git restore .`, `git clean -f`) without explicit developer instruction.

## Branching Model

- `develop` is the integration branch. Feature branches -> `develop` is **squash-only**; develop is kept linear.
- `develop` -> `main` is **merge-commit only** (no squash, no rebase). Merge commits preserve develop's commit list as a real second-parent reference on main, which lets the release model attribute releases to the develop commits that produced them (relevant both for the weekly publish and the opt-in `PUBLISH_ON_MERGE` mode - see "Release Model" below). Branch protection enforces this: the develop ruleset allows only `squash`, the main ruleset allows only `merge`.
- All commits on both branches must be cryptographically signed (SSH or GPG). Squash and merge commits created via the GitHub UI are signed by GitHub's web-flow key.
- **`develop` is forward-only - no `main -> develop` back-merges.** The develop ruleset's squash-only setting physically blocks merge commits on develop. Historical back-merge commits visible in `git log` (`b9b0447`, `410ba56`, `ffb9e64`, `5ce95cf`, etc.) predate this rule and must not be repeated.
- **Both rulesets intentionally omit "Require branches to be up to date before merging".** The flag is off on `main` and on `develop`, for related but distinct reasons.
  - *Main:* the check is graph-based - it asks whether main's tip commit is reachable from develop, not whether the two branches have the same content. After any develop -> main release, main's tip is a brand-new merge commit that develop's history doesn't contain. Forward-only develop never adds it (no back-merge of main into develop), so the check would fail on every subsequent release. Other technical workarounds - rebasing develop onto main, or rewriting develop's history - exist but contradict the squash-only develop ruleset and the linearity invariant.
  - *Develop:* the check stalls bot auto-merge when two bot PRs against develop land within the same window. As soon as the first merges, the second flips to `mergeStateStatus: BEHIND` and GitHub's auto-merge will not fire while strict is on. The merge-bot only *enables* auto-merge on `opened`/`reopened` (see below) and never auto-updates bot branches, and Dependabot's rebase isn't real-time, so the second PR sits OPEN with all checks green indefinitely. Squash mechanics still rebase the diff onto develop's tip on merge, `required_linear_history` still enforces linearity, textual conflicts still block `mergeable: CONFLICTING`, and the required `Check pull request workflow status` still gates merges - the only thing lost is pre-merge detection of *semantic-but-not-textual* conflicts, which the post-merge develop CI run catches anyway.
  - See [`README.md`](./README.md#template---github-setup) "Rules / Rulesets" for the configured state.
- **Configuring branch protection on a derived repo: don't hand-build the rules.** Reconstructing the rules by hand is error-prone and has gone wrong on past ports. First delete **all** legacy classic branch-protection rules and any stray rulesets (this template uses rulesets *only*), then create **exactly two rulesets named `develop` and `main`** by exporting the template's two rulesets and re-importing them via `gh api -X POST .../rulesets` (`gh ruleset` is read-only). The names are load-bearing - this file and the workflows reference them. Full export/import procedure: [README "Rules / Rulesets"](./README.md#template---github-setup). **Brownfield repos** (pre-existing history) need an extra step: `Require signed commits` rejects legacy unsigned commits and the admin bypass does not cover `git push --force`, so re-signing requires temporarily disabling the ruleset - see the [brownfield migration procedure](./README.md#template---github-setup) in that section.
- **Bots (Dependabot and codegen) target both `main` and `develop` in parallel.** [`.github/dependabot.yml`](./.github/dependabot.yml) duplicates every ecosystem entry (one per branch) and [`.github/workflows/run-codegen-pull-request-task.yml`](./.github/workflows/run-codegen-pull-request-task.yml) runs as a matrix over both branches with branch names `codegen-main` and `codegen-develop`. Each branch absorbs its own bot PRs independently, so neither falls behind, and the forward-only rule still holds (nothing is back-merged from main to develop - both branches receive their updates directly). The merge-bot ([`.github/workflows/merge-bot-pull-request.yml`](./.github/workflows/merge-bot-pull-request.yml)) dispatches `--squash` or `--merge` from each PR's base ref via a `case` statement so the form matches the ruleset on either base. Dependabot **security** PRs (CVE-driven) always open against the repo default branch (`main`) regardless of `target-branch` - the same `case` statement covers them.
- **Maintainer-pushed commits on a bot PR auto-disable auto-merge.** The merge-bot's `merge-dependabot` and `merge-codegen` jobs only fire on `opened` / `reopened` events (auto-merge is enabled exactly once per PR). When a maintainer pushes commits to a bot's branch (a `synchronize` event with an actor that isn't the same bot), the merge-bot's `disable-auto-merge-on-maintainer-push` job fires and calls `gh pr merge --disable-auto`. The maintainer's commits stay in the PR but won't auto-merge with the bot's content; re-enable auto-merge manually (`gh pr merge --auto <PR>` or the GitHub UI) when ready.
- **Why parallel dual-target rather than develop-only with eventual flow-through:** push-distribution channels (HACS for Home Assistant integrations, Linux distros that vendor from `main`, etc.) consume `main` directly. A develop-only model would leave `main` running stale code during long-running develop features. Codegen content can also be production-critical (live API-derived data, language lists, build catalogs) rather than just sample/demo content, so both branches need fresh codegen on their own cadence.
- **Dual-target codegen + per-run state = merge conflicts.** If a generator embeds per-invocation state (timestamps, GUIDs, build IDs) and runs independently on `main` and `develop`, the two branches' outputs diverge and every `develop -> main` release conflicts on the generated file. This template's `CodeGen/CodeGen.cs` demo embeds a timestamp; the codegen workflow captures one UTC timestamp in a `get-runtime` job and passes it via `--runtime` to both matrix legs so they produce byte-identical output. (It deliberately does *not* use `github.run_started_at`, which resolves to an empty string in the reusable-workflow context and made each leg fall back to its own `DateTime.UtcNow` - diverging the output and reintroducing the conflict.) **That `--runtime` plumbing is template hygiene only - not a codegen pattern derived projects should reproduce.** Your real generators should either be deterministic given the same inputs (preferred), or not run on both release branches simultaneously, or absorb the per-release merge cost.
- **App-token workflows use Client ID, not App ID.** `actions/create-github-app-token` deprecated the numeric `app-id` input in v3.0.0; the template uses `client-id: ${{ secrets.CODEGEN_APP_CLIENT_ID }}`. When adding new App-token call sites, use the same form - do not reintroduce `app-id` / `CODEGEN_APP_ID`. See [README "Template - GitHub Setup"](./README.md#template---github-setup) for the secret-setup procedure.

## Release Model

The template uses a **two-phase model by default**: PRs build fast, publishing is batched. See [README "Release Distribution Model"](./README.md#template---release-distribution-model-two-phase-by-default) for the full rationale; the load-bearing rules:

- **PRs smoke-test only.** [`test-pull-request.yml`](./.github/workflows/test-pull-request.yml) always runs unit tests, then a `dorny/paths-filter` `changes` job gates a **reduced** build of only the changed targets (Docker `linux/amd64` only, executable on a representative runtime subset), never pushing. Build-workflow files are intentionally not in the path filters - a filter can't tell a logic change from an action-version bump - so a workflow-only change isn't smoke-built; the reusable workflows are exercised by the next run that uses them (a later code PR's smoke build, or the scheduled/publish run). There is no CI workflow-lint job; lint workflow edits with `actionlint` locally before pushing.
- **Merges don't publish by default.** [`publish-release.yml`](./.github/workflows/publish-release.yml) is the sole publisher: its **weekly schedule** (Mondays 02:00 UTC) and **manual `workflow_dispatch`** always do the full build/publish of **both** `main` and `develop` (a branch matrix). Its `push` trigger publishes only when the **`PUBLISH_ON_MERGE` repository variable** is `true` (opt-in legacy continuous-release). Unset/`false` = two-phase.
- **Required check.** The `changes` job is in the `Check pull request workflow status` aggregator's `needs` and **must succeed** (not just "not fail") - a paths-filter error must never let a target-changing PR merge with its smoke build silently skipped. Skipped smoke jobs (no matching change) pass; `failure`/`cancelled` blocks.
- **Reusable-task parameter contract.** Every `build-*-task.yml` and `build-release-task.yml` takes `ref` (git ref to check out/version), `branch` (logical branch driving config/tags/prerelease - `main` => Release/`latest`/non-prerelease, else Debug/`develop`/prerelease), and where relevant `smoke`. **Branch-derived config keys off `inputs.branch`, never `github.ref_name`** - the publisher's matrix builds `develop` from a run whose `github.ref_name` is `main`, so `ref_name` would be wrong. Artifact names are branch-suffixed so both matrix legs coexist in one run. `get-version-task.yml` takes a `ref` so NBGV versions the right branch.
- **Per-target subsetting (derived projects).** `build-release-task.yml` has per-target `enable_*` gates and self-contained leaf tasks, so a project that drops a target deletes: its `build-<target>-task.yml`, the matching job + `github-release` `needs` entry in `build-release-task.yml`, its path-filter entry in `test-pull-request.yml`, and (for PyPI) the `publish-pypi` job in `publish-release.yml`. CodeGen, versioning, badge, merge-bot, and Dependabot are target-agnostic.
- **Orchestration vs. build - the override seam.** The pipeline splits into two layers. The **orchestration** layer is generic and meant to be synced verbatim from upstream: [`publish-release.yml`](./.github/workflows/publish-release.yml) (publish plan + branch matrix), the `get-version` + `github-release` jobs inside [`build-release-task.yml`](./.github/workflows/build-release-task.yml), [`get-version-task.yml`](./.github/workflows/get-version-task.yml), [`build-datebadge-task.yml`](./.github/workflows/build-datebadge-task.yml), and the aggregator shape of [`test-pull-request.yml`](./.github/workflows/test-pull-request.yml). The **build** layer - the `build-<target>-task.yml` leaf tasks - is what a derived project owns and replaces. The contract that keeps the seam clean: **a target contributes files to the GitHub release by uploading a workflow artifact named `release-asset-<branch>-<target>`.** The `github-release` job collects every `release-asset-<branch>-*` artifact by pattern and **never names a build job**, so it (the tag-the-commit + create-the-release + attach-the-assets logic) is reusable **verbatim** - that is the part a downstream previously had to fork and rewrite, and no longer does.
  - **What a downstream still curates** (this is by design, not a leak): the *list* of leaf jobs in `build-release-task.yml`. Per **Per-target subsetting** above, you delete the target jobs you don't ship and add the one(s) you do - `build-release-task.yml`'s `github-release` job is untouched, but the file is not byte-identical because its `needs`/job list reflects your targets. Making that list itself target-agnostic is a larger "factor build from orchestration" refactor that is intentionally **not** done.
  - **Map your outputs to the right seam** - pick by where each artifact *goes*, not by language:
    - *Files attached to the GitHub Release* (zips, binaries, packaged libraries): one leaf task per output, each uploading `release-asset-<branch>-<name>`. A data-only repo (e.g. a symbol library) has exactly one such task: validate -> `zip` -> upload `release-asset-<branch>-library`; it deletes the nuget/pypi/executable/docker jobs and the `publish-pypi` job, keeps `github-release` as-is. This is also where the .NET `build-executable-task` lives - it is *not* a generic file step, it is specifically `dotnet publish` of the console app; replace it wholesale, don't adapt it.
    - *Package-registry pushes* (NuGet.org, PyPI): the leaf task both builds **and** publishes to its registry. NuGet pushes from inside `build-nugetlibrary-task` (`dotnet nuget push --skip-duplicate`) *and* also uploads a `release-asset-*` (.7z) for the GitHub release. PyPI is split: `build-pypilibrary-task` only builds + uploads the `pypilibrary-build-<branch>` artifact, and the separate `publish-pypi` job in `publish-release.yml` does the OIDC Trusted-Publishing upload (so `id-token: write` is granted only at that one entry point) - PyPI contributes **no** `release-asset-*`.
    - *Image-registry pushes* (Docker Hub): `build-docker-task` pushes multi-arch tags directly; contributes **no** `release-asset-*`.
    - *Source-only / no build* (validate + tag + release): you need none of the package/image leaf tasks - only your validation in `test-pull-request.yml`, one `release-asset-*` leaf task for the artifact you attach (or zero, if the release is just a tag), and the verbatim `get-version` + `github-release` + `date-badge` orchestration.
  - `get-version-task.yml` installs the .NET SDK only because NBGV needs the runtime to compute the version/tag - heavyweight but expected even for a non-.NET downstream; acceptable as-is.
- **No-op republish guarantee.** A weekly/dispatch publish where NBGV `SemVer2` is **unchanged** (no new commit since the last publish) re-pushes **nothing** to GitHub Releases (the `github-release` job's `release-exists` check skips the create step), NuGet (`dotnet nuget push --skip-duplicate`), or PyPI (`gh-action-pypi-publish` `skip-existing: true`) - all three key on the version string. **Docker always re-pushes** by design: it picks up upstream base-image refreshes (e.g. `ubuntu:rolling`) that aren't visible in the repo. Boundary: `version.json` has **no `pathFilters`**, so *any* commit - including a CI/workflow-only or docs-only change - advances the NBGV git height and therefore `SemVer2`, and the next publish *does* create a fresh release for it even when the shipped binary is byte-identical. This is accepted NBGV behavior; `pathFilters` are intentionally not added.
- **Versioning is semantic and maintainer-controlled.** The `version` (major.minor) in [`version.json`](./version.json) is the version floor; NBGV appends the git height (the SemVer patch position) for the build version. `main` (the public release ref) builds a stable `X.Y.<height>`; `develop` builds a prerelease `X.Y.<height>-g<sha>`. The maintainer edits `version.json`; dependency bumps, CI/workflow fixes, doc edits, and template re-syncs leave it untouched.
  - **Bump `version.json` only for functional changes, by maintainer instruction.** Raise the major/minor when the work being introduced warrants a new semantic version - a new feature, a behavior or API change, a breaking change - and do it in the PR that introduces that work (typically on `develop`). Do **not** bump on a fixed cadence or mechanically after a release. NBGV advances the patch (git height) on every commit automatically, so a release always gets a fresh build version without any `version.json` edit.
  - **`develop` need not lead `main`; no post-release bump.** Because `develop` builds are always prereleases (`X.Y.<height>-g<sha>`), they are ordered below same-`X.Y` stable releases *by design* - that is what "prerelease" means - so there is no need to keep `develop` a minor ahead, and no `bump-version-X.Y` PR after a release. `develop` *may* sit at a higher major/minor than `main` whenever functional work in flight has bumped it, but that is incidental, not a requirement. A `develop -> main` promotion simply carries whatever `version.json` is current: a promotion that introduced a functional bump releases that new version on `main`; a maintenance-only promotion (dependency bumps, CI/doc fixes, template re-syncs) carries the unchanged `version.json` and `main` advances only its NBGV height.

## Pull Request Title and Commit Message Conventions

### Format

- Imperative subject summarizing the change, <=72 characters, no trailing period. ("Add 24-hour PM2.5 average sensor", not "Added X" or "Adds X".)
- Optional body, blank-line separated, explaining *why* the change is being made when that's non-obvious. The diff shows *what*.

### Rules

- Don't write `update stuff`, `wip`, or other vague titles. (Dependabot's default `Bump X from Y to Z` titles are fine - keep them.)
- Don't add `Co-Authored-By:` lines unless the developer explicitly asks.
- Don't put release-bump magnitude in the title - no "minor", "patch", "release v0.2.0", etc. Nerdbank.GitVersioning computes the next release version from `version.json` + git history. Dependency versions in dependency-bump titles are fine and expected.
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
- **Write docs in the current state, not as a change from a prior one.** The reader has no memory of the previous behavior, so describe what *is*: "X does Y", never "X *now* does Y", "X *no longer* does Z", "changed/switched/restored to Y", or "X *still* does W". Before/after framing belongs in changelogs, commit messages, and PR descriptions - where the prior state is the point - not in `README.md` or other living docs.

### Character Set

- **Write ASCII in all agent-authored text** - documentation, code, comments, commit messages, and PR descriptions. The agent does not introduce non-ASCII characters. Replace typographic Unicode with its ASCII equivalent on sight:
  - em dash (U+2014) and en dash (U+2013) -> hyphen `-` (use a spaced ` - ` for an em-dash-style clause break)
  - right arrow (U+2192) -> `->`; double arrow (U+21D2) -> `=>`
  - less-than-or-equal (U+2264) -> `<=`; greater-than-or-equal (U+2265) -> `>=`
  - curly quotes (U+2018/U+2019/U+201C/U+201D) -> straight `'` and `"`; ellipsis (U+2026) -> `...`
- **Allowed non-ASCII (two narrow exceptions):**
  - **Scientific or technical symbols with no clean ASCII equivalent** - e.g. ohm, micro, degree, pi. Keep the symbol; do not approximate it away.
  - **Unicode the developer deliberately typed** - emoji used for emphasis or as callout markers (for example the warning/info markers a maintainer placed in `README.md`). Preserve it; never strip the developer's own characters. This carve-out is for developer-authored text, not a license for the agent to add emoji.

### Line Endings

- **[`.editorconfig`](./.editorconfig) defines the correct line ending per file type:** **CRLF** for `.md`, `.cs`, XML/`.csproj`/`.props`/`.targets`, `.yml`/`.yaml`, `.json`, and `.cmd`/`.bat`/`.ps1`; **LF** for `.sh`. `.gitattributes` is `* -text`, so git stores the exact bytes you commit and will **not** normalize endings for you.
- **New files:** create them with the `.editorconfig`-mandated ending.
- **Editing an existing file:** **preserve the file's current line endings** - do not reflow them as a side effect of a content change, even if the file is already non-compliant. A tool that rewrites a file in text mode (a script, a bulk find/replace) can silently flip CRLF to LF and turn a one-line change into a whole-file diff. After any programmatic edit, verify before staging: `git diff --stat` should touch only the lines you changed, and `file <path>` should report the file's expected ending. If a diff balloons to the whole file, you flipped the endings - restore them and re-stage.
- **Fixing a non-compliant file:** bring it to its `.editorconfig` ending as a **deliberate** change, and prefer to isolate it in its own EOL-only commit so the churn is reviewable. When a broader maintenance change has to normalize endings alongside content edits (a repo-wide cleanup sometimes does), call it out explicitly in the commit/PR description and verify the content separately with `git diff --ignore-cr-at-eol`.
- **Derived repos must carry both files.** [`.editorconfig`](./.editorconfig) **and** [`.gitattributes`](./.gitattributes) are mandatory verbatim carries (see [Files and Sections Derived Repos Must Carry Verbatim](#files-and-sections-derived-repos-must-carry-verbatim)). A derived repo missing either file, or one whose `.editorconfig` sets `end_of_line` only under `[*.md]` instead of carrying the full per-extension rules, will accumulate files mixed between LF and CRLF - the exact failure these two files prevent.

### Quantitative Claims

- Any quantitative claim in `README.md` (counts, sizes, version floors, supported platforms) must be verified against current code. If a doc number is derived from a code constant, mark the dependency in a source-code comment so the next editor knows to update both.

## PR Review Etiquette

> **Mandatory in every derived repo.** This entire "PR Review Etiquette" section is the provider-agnostic review-loop *contract* and must be carried **verbatim** into every repo derived from this template, alongside the [`.github/copilot-instructions.md`](./.github/copilot-instructions.md) "GitHub Copilot Review Runbook" that implements it. Without both in-repo, an agent working in the derived repo has no pointer to the reliable Copilot mechanics and falls back to ad-hoc (and known-broken) behavior. See [Files and Sections Derived Repos Must Carry Verbatim](#files-and-sections-derived-repos-must-carry-verbatim).

The repo runs a review loop on every PR: local agent iteration plus remote automated review (GitHub Copilot is the configured reviewer). Treat this as a contract regardless of which local agent authored the changes.

### Expected Review Loop

1. Push changes to the PR branch.
2. Re-request a review for the **current head SHA**. Auto-trigger is unreliable, so request it explicitly via the `requestReviews` GraphQL mutation (now reliable end-to-end - see the runbook); the UI is only a fallback.
3. Wait for review activity on that head.
4. Triage findings.
5. Apply fixes or write a rationale for declines.
6. Reply to each thread and resolve what was addressed.
7. Re-run the loop after every fix push until no actionable findings remain.

`mergeStateStatus: CLEAN` only checks required statuses; it does not block on bot review comments. Drive the loop to green - review confirmed on the latest head SHA and every actionable finding closed - and then **wait for the maintainer's explicit permission to merge**. The agent does not merge on its own (consistent with "default to staging"; merging is maintainer-authorized).

For provider-specific mechanics (how to request review, query review state, post replies, resolve threads), see the **GitHub Copilot Review Runbook** in [.github/copilot-instructions.md](./.github/copilot-instructions.md). This file owns the contract; that file owns the mechanics.

### Triaging Review Comments

For each comment, classify before responding:

- **Bug** - wrong behavior, missing test coverage, or a real divergence between code and docs. Fix it. Reply with the fixing commit SHA when done.
- **Style/convention** - the comment cites a rule from this file or a language-specific style guide. Two cases:
  - The cited rule matches what the existing codebase already does -> fix the offending code.
  - The cited rule contradicts what's in the tree, or industry norm -> **update the rule instead of the code**. The rule is wrong, not the code. Bouncing the same code across rounds is the symptom of a wrong rule. Heuristic: three rounds on the same style category means the rule needs adjusting and the user should authorize the rule change.
- **Architectural opinion** - the comment proposes a different design ("constrain this to disabled-by-default", "move it elsewhere", "add a runtime guardrail"). This is judgment, not a bug. Surface it to the user with a recommendation; don't apply unilaterally.

### Responding and Resolution Expectations

Reply inline with either the fixing commit SHA (for accepted issues) or a concise rationale (for declines). Resolve review threads when addressed or intentionally declined with rationale. Issue-level comments (those at `repos/.../issues/<N>/comments` rather than tied to a specific line) have no resolution action - acknowledge with a reply if needed and move on.

After the final push on a PR, sweep older threads from earlier rounds whose code paths no longer exist; otherwise stale unresolved markers remain in the review UI.

### Escalating to the User

Bring the user in when:

- **Genuine design trade-off** surfaces (fail-open vs fail-closed, narrow vs broad refactor scope, "should we add a guardrail or trust the docstring"). Triage, recommend, ask.
- **Repeated friction** across rounds without convergence - that's the rule-needs-updating signal. Stop, summarize the pattern, and let the user authorize the rule change.
- **Architectural redesign** is requested rather than a bug fix. Surface with a recommendation; never apply unilaterally.

Anti-pattern: don't keep flipping the code on the same style point. Flip the rule once and stick to the rule.

## Workflow YAML Conventions

These conventions describe the target state. New and modified workflows must respect them; the rest of the repo is expected to be brought up to the same standard. Sweep PRs that apply a rule everywhere are welcome when a rule changes.

- **Action pinning**: pin **every** action - first-party (`actions/*`) and third-party - to a commit SHA with a trailing `# vX.Y.Z` comment, so Renovate / Dependabot can still bump it but a tag swap can't change the executed code. Use `# vX` (major-only) only when the upstream's floating major tag doesn't correspond to a specific patch/minor release SHA - pinning to the floating-tag SHA still gives the SHA guarantee, the version comment just records the major line. Documented exception (no SHA pin at all): [`dotnet/nbgv`](./.github/workflows/get-version-task.yml) is consumed via `@master` because the upstream tag stream lags `master` substantially and Dependabot's tag-tracking would propose a downgrade - the rationale is documented inline in that workflow.
- **Filename**: reusable workflows (those with `on: workflow_call`) end in `-task.yml`. Entry-point workflows (`on: push` / `pull_request` / `schedule` / `workflow_dispatch`) do NOT use the `-task` suffix; they end with what they do - `-pull-request.yml`, `-release.yml`, etc. The suffix carries semantic meaning: a `-task.yml` file is meant to be `uses:`-d, never triggered directly.
- **Workflow `name:`** (the top-level `name:` field): reusable workflow names end in **"task"** (e.g. `Build PyPI library task`); entry-point workflow names end in **"action"** (e.g. `Publish project release action`, `Test pull request action`). The displayed action name in the GitHub Actions UI tells you at a glance whether you're looking at an orchestrator or a callee.
- **Job and step `name:` suffixes**: every job's `name:` ends in **"job"**; every step's `name:` ends in **"step"**. **Exception**: a job whose `name:` is also referenced as a required-status-check `context:` in a branch ruleset (currently `Check pull request workflow status` in `test-pull-request.yml`) keeps the ruleset-bound name verbatim - renaming would silently break required-status-check enforcement. Do not "fix" that name; if a future job becomes ruleset-bound, mark it the same way.
- **Concurrency**: top-level workflows declare `concurrency: { group: '${{ github.workflow }}-${{ github.ref }}', cancel-in-progress: true }` so a fresh push supersedes an in-flight run on the same ref. **Documented exceptions** (both record the rationale inline in their header comment): (1) [`merge-bot-pull-request.yml`](./.github/workflows/merge-bot-pull-request.yml) uses `cancel-in-progress: false` because its three-job model (enable-auto-merge on opened, disable-auto-merge on maintainer-pushed synchronize, with method dispatched by base) requires each event to run to completion in arrival order - cancellation would leave auto-merge in an inconsistent state. (2) [`publish-release.yml`](./.github/workflows/publish-release.yml) uses both a **global, ref-independent group** (`group: ${{ github.workflow }}`, dropping the usual `-${{ github.ref }}`) and `cancel-in-progress: false`. It publishes shared ref-independent artifacts (both branches' Docker tags/caches and GitHub releases) on schedule/dispatch regardless of the triggering ref, so a ref-scoped group would let a scheduled run (ref `main`) and a manual dispatch (ref `develop`) run concurrently and double-push; and cancelling a publish mid-flight can leave a partially pushed tag set or a half-created release. The global group + queueing serializes every publish run to completion.
- **Shells**: multi-line `run:` blocks with bash start with `set -euo pipefail` - fail fast, fail on undefined vars, fail on a failed pipe segment.
- **Conditionals**: multi-line `if:` uses folded scalar `if: >-` so YAML preserves whitespace correctly. Literal block (`if: |`) is wrong because it embeds newlines inside the boolean expression.
- **Boolean inputs**: workflows triggered both via `workflow_call` and `workflow_dispatch` must declare each boolean input in *both* trigger blocks - one definition does not propagate to the other. `workflow_call` delivers booleans as actual booleans; `workflow_dispatch` delivers them as the *strings* `"true"`/`"false"`. Any `if:` consuming a boolean input must compare against both forms - `if: ${{ inputs.foo == true || inputs.foo == 'true' }}`.
- **Reusable workflows**: job-level `permissions:` are validated *before* the `if:` evaluates, so even a skipped job needs valid permissions declared. A `release` job with `permissions: contents: write` and `if: ${{ inputs.publish }}` will still cause `startup_failure` on a caller that doesn't grant `contents: write`. Either declare permissions at the call site, or omit the inner block and inherit.
- **Allowlist `success` and `skipped` explicitly** when chaining jobs across optional dependencies - `!= 'failure'` lets `cancelled` through (timeout, runner failure, manual cancel). Use `(needs.X.result == 'success' || needs.X.result == 'skipped')`.
- **Tag pinning on releases**: when using `softprops/action-gh-release` (or any tag-creating action), pass `target_commitish` explicitly - without it, GitHub's REST API defaults the new tag to the repository's default branch instead of the commit that built the artifact. Pin it to the **exact built commit's SHA** (the publisher uses NBGV's `GitCommitId` output), not `github.sha` (wrong branch in the publisher's branch matrix - a `develop` leg runs with `github.sha` = main's tip) and not a branch name (a moving ref that a mid-run commit could advance past the built tree).

### Running the Linters Locally (Known-Working Invocations)

There is no CI lint job for workflow YAML or Markdown - the gate is local, so an agent must know how to actually run these tools. Some linters are not obvious to invoke and their non-Docker install paths (curl-pipe installers, global npm) are frequently blocked in sandboxes or fail on WSL. **Prefer the Docker invocations below; they are the known-working path and need no local toolchain.** Both tools auto-discover their targets from the working directory.

- **actionlint** (GitHub Actions workflow YAML - run after any `.github/workflows/` edit, since workflow-only changes are not smoke-built):

  ```sh
  docker run --rm -v "$PWD":/repo --workdir /repo rhysd/actionlint:latest -color
  ```

  The `rhysd/actionlint` image bundles `shellcheck`, so it also validates `run:` shell blocks. The direct-binary/curl-installer path is often sandbox-blocked - use Docker.

- **markdownlint-cli2** (Markdown - mirrors the davidanson VS Code extension via the shared [`.markdownlint-cli2.jsonc`](./.markdownlint-cli2.jsonc), so the CLI and IDE agree):

  ```sh
  docker run --rm -v "$PWD":/workdir davidanson/markdownlint-cli2:latest "**/*.md"
  ```

  In a configured editor the davidanson extension is enough; use the Docker CLI when there's no IDE (agent/headless) or to confirm a clean run before pushing.

When pulling a public image fails on a Docker-Desktop/WSL credential-helper error (`docker-credential-desktop.exe: exec format error`), retry with an empty Docker config: `DOCKER_CONFIG=$(mktemp -d) docker run ...` after writing `{}` to `$DOCKER_CONFIG/config.json`.

## Devcontainer

The repo ships **two per-language devcontainers** so each container carries only one toolchain (and the matching VS Code extensions): [`.devcontainer/dotnet/devcontainer.json`](./.devcontainer/dotnet/devcontainer.json) (.NET 10 SDK) and [`.devcontainer/python/devcontainer.json`](./.devcontainer/python/devcontainer.json) (Python 3.14 + version-pinned `uv`). Open [`DotNet.code-workspace`](./DotNet.code-workspace) or [`Python.code-workspace`](./Python.code-workspace) and pick **Reopen in Container** to land in the matching one.

Both containers bind-mount the host SSH signing key's *public half* (`~/.ssh/id_ed25519.pub`), `~/.config/git/allowed_signers`, and `~/.config/gh` so commits inside the container are SSH-signed (signing happens via the forwarded `ssh-agent` socket - the private key never enters the container) and, *when the host's `gh` token is file-backed*, `gh` is pre-authenticated. On Keychain (macOS) or libsecret (Linux) hosts, `~/.config/gh/hosts.yml` carries no `oauth_token`, so container `gh` is unauthenticated until the contributor opts into `gh auth login` inside the container. See [docs/devcontainer.md](./docs/devcontainer.md) for full setup, [docs/host-setup.md](./docs/host-setup.md) for prerequisites, and [docs/ssh-signing.md](./docs/ssh-signing.md) for the SSH commit signing details.

Each devcontainer's `customizations.vscode.extensions` mirrors the `recommendations` array in its matching workspace file - when you add an extension to one, add it to the other.

## Project Structure (Languages)

- **.NET projects** (build with `dotnet build`, test with `dotnet test`):
  - `NuGetLibrary/` - core reusable .NET NuGet library (published as `ptr727.ProjectTemplate.Library`)
  - `Console/` - CLI app using System.CommandLine
  - `Tests/` - xUnit + AwesomeAssertions
  - `Benchmarks/` - BenchmarkDotNet
  - `CodeGen/` - internal codegen tooling
  - **Style guide: [`CODESTYLE.md`](./CODESTYLE.md)**.
- **Python project** (env/build/test with `uv` from inside `PyPiLibrary/`):
  - `PyPiLibrary/` - PyPI library template, published as `ptr727-projecttemplate-library`
  - **Style guide: [`PyPiLibrary/CODESTYLE.md`](./PyPiLibrary/CODESTYLE.md)**.
- **Cross-cutting**:
  - `.github/` - workflows, Dependabot, Copilot instructions
  - `.devcontainer/dotnet/` and `.devcontainer/python/` - per-language devcontainer configs + post-create scripts
  - `DotNet.code-workspace`, `Python.code-workspace` - per-language VS Code workspace files (each pairs with its devcontainer)
  - `.vscode/` - debug configs and tasks (.NET-oriented)
  - `Docker/` - multi-platform Linux container build for the Console app

When you touch code in either language, also respect that language's style guide. Conventions in this file (PR titles, branching, US English, devcontainer behavior, workflow YAML) apply uniformly to both languages.

## Quick Start for Derived Projects

1. **Clone this template** as the baseline for your project.
2. **Decide** which language sides you need. If you need only one, delete the other folder and its references - see the relevant CODESTYLE for the deletion checklist.
3. **Read** [CODESTYLE.md](./CODESTYLE.md) (.NET) and/or [PyPiLibrary/CODESTYLE.md](./PyPiLibrary/CODESTYLE.md) (Python) for the per-language style.
4. **Carry the mandatory shared files and sections verbatim** - do not re-invent them per repo. See [Files and Sections Derived Repos Must Carry Verbatim](#files-and-sections-derived-repos-must-carry-verbatim) for the exact list (review-loop contract + runbook, lint config, line-ending governance) and what to adapt.
5. **Update project-specific values** - `PackageId`/`RootNamespace` in `.csproj`, `name` in `pyproject.toml`, namespace conventions, `README.md`, `HISTORY.md`, `version.json`, `LICENSE`, NuGet/PyPI badge URLs.
6. **Run tools before first commit**:
   - .NET: `dotnet tool restore`.
   - Python: `cd PyPiLibrary && uv sync`.
   - Optional pre-commit hooks (off by default) - see README "Optional: enable git hooks locally".
7. **Wire up release credentials** when ready to publish - see the README's release notes section and [PyPiLibrary/README.md](./PyPiLibrary/README.md) for PyPI Trusted Publisher setup.

### Files and Sections Derived Repos Must Carry Verbatim

These artifacts are the template's cross-cutting contract. A derived repo must carry **each** of them; copy the file/section as-is and change only the noted placeholders. Re-inventing or omitting any of these is the drift the template exists to prevent.

- **[`AGENTS.md`](./AGENTS.md) "PR Review Etiquette" section** - the provider-agnostic review-loop contract. Copy verbatim. No placeholders to change (it names no owner/repo).
- **[`.github/copilot-instructions.md`](./.github/copilot-instructions.md)** - the whole file is a drop-in; its "GitHub Copilot Review Runbook" carries the provider mechanics. Copy verbatim and change only the `<owner>` / `<repo>` / `<N>` placeholders in the API snippets; drop language-specific style pointers that don't apply.
- **[`.markdownlint-cli2.jsonc`](./.markdownlint-cli2.jsonc)** - the shared lint config read by both the davidanson `markdownlint` IDE extension and CLI/CI `markdownlint-cli2`, so the IDE and command line stay in lock-step. Copy verbatim (it is repo-agnostic). **On first adoption**, a repo's existing docs often carry structural debt this config surfaces (MD022/MD031/MD032 blank lines around headings/fences/lists, MD040 unlabeled fences). Clear it in one pass by running the markdownlint-cli2 Docker command from [Running the Linters Locally](#running-the-linters-locally-known-working-invocations) with `--fix` added (`docker run --rm -v "$PWD":/workdir davidanson/markdownlint-cli2:latest --fix "**/*.md"`), then hand-label any remaining unlabeled fences (MD040 - usually `text` for format/example blocks) and **re-verify the line endings of touched `.md` files** (`--fix` can rewrite a CRLF file as LF).
- **[`.editorconfig`](./.editorconfig) and [`.gitattributes`](./.gitattributes)** - line-ending governance (see [Line Endings](#line-endings)). Copy **both** verbatim. `.editorconfig` sets `end_of_line` per file type and `.gitattributes` (`* -text`) stops git from normalizing; a repo missing either, or one that only sets `end_of_line` for `[*.md]` instead of carrying the full per-extension rules, drifts between LF and CRLF.

When the template changes one of these, re-sync the derived repo from the new version (see below).

### Staying in Sync and Reporting Drift Upstream

A derived repo is expected to **re-sync against the template periodically**, not just at creation: pull the current version of each verbatim-carry artifact above and re-apply it (adapting only the noted placeholders).

**Drift flows back upstream as an issue, not a private fix.** When porting or re-syncing, if you find a discrepancy that should be fixed in the **template itself** - a gap, an outdated instruction, a missing rule, something that bit this repo and would bite the next derived repo too - **open an issue in [`ptr727/ProjectTemplate`](https://github.com/ptr727/ProjectTemplate)** describing it, rather than only patching it locally. A local fix realigns *this* repo; an upstream issue (then fix) corrects it *for every future derived repo* and keeps the template the single source of truth. This is exactly how the current review-loop / lint-config / brownfield-migration gaps were surfaced.

#### Known Downstream Projects

Sync is **bidirectional**. The flow above is the downstream-to-upstream direction (derived repos report drift up). The reverse direction is the maintainer's: **when changing a verbatim-carry artifact or another cross-cutting contract in this template, file a heads-up issue in each affected downstream repo below** so it can re-sync, rather than letting the change be discovered only on the next ad-hoc port. Keep this table current as projects are derived from or retired off the template.

| Repo | Ships | Consumer model |
| --- | --- | --- |
| [`ptr727/NxWitness`](https://github.com/ptr727/NxWitness) | Network Optix VMS Docker images | pull (Docker Hub) |
| [`ptr727/PlexCleaner`](https://github.com/ptr727/PlexCleaner) | Media-optimization tool (Docker + release binaries) | pull (Docker Hub / GitHub releases) |
| [`ptr727/Utilities`](https://github.com/ptr727/Utilities) | C# .NET utility library | pull (NuGet) |
| [`ptr727/LanguageTags`](https://github.com/ptr727/LanguageTags) | ISO 639 / BCP 47 language-tag library | pull (NuGet) |
| [`ptr727/ESPHome-NonRoot`](https://github.com/ptr727/ESPHome-NonRoot) | Non-root ESPHome Docker image | pull (Docker Hub) |
| [`ptr727/VSCode-Server-DotNetCore`](https://github.com/ptr727/VSCode-Server-DotNetCore) | VS Code Server + .NET SDK Docker image | pull (Docker Hub) |
| [`ptr727/homeassistant-purpleair`](https://github.com/ptr727/homeassistant-purpleair) | PurpleAir Home Assistant integration | push (HACS) |

The *consumer model* column (push = forced update on every release, pull = consumer updates on its own cadence) is the signal that drives a downstream's two-phase vs `PUBLISH_ON_MERGE` choice - see [README "Release Distribution Model"](./README.md#template---release-distribution-model-two-phase-by-default).

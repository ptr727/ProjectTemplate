# AUDIT.md

How an agent audits a repository against the fleet ground truth in this repo and reports drift. This is the procedure; the ground truth it checks against is [`registry/repos.json`][repos], the [`spec/`][spec] manifests, [`repo-config/`][repo-config], and the prose authorities ([`AGENTS.md`][agents], [`CODESTYLE.md`][codestyle], [`WORKFLOW.md`][workflow]). The audit is read-only: it produces a report under [`reports/`][reports], never edits the target repo.

The verdict vocabulary is [`WORKFLOW.md`][workflow]'s: **operational / not operational**, **N/A**,
**defect**, and the applicable/absent rule. Do not invent a parallel scheme.

## 1. Scope and Ground-Truth Branch

Audit one repository at a time. Read the target's **`main` branch** as ground truth: `main` is the released, authoritative state. Read `develop` only to detect divergence - a stale or diverged `develop` (behind `main`, or diverged) is reported as a **drift finding**, never audited as the truth. Do not treat a `develop`-only file as present if it is absent on `main`.

## 2. Resolve the Repo's Type(s)

Look up the repo in [`registry/repos.json`][repos] and read its `types[]`. If the entry is `classificationPending` (a backlog repo), classify it from the tree and propose a registry update:

- `*.csproj` / `*.slnx` -> `csharp`; a `dotnet nuget push` workflow -> `nuget`; a `System.CommandLine` console -> `console`.
- `pyproject.toml` / `setup.py` -> `python`; a `pypa/gh-action-pypi-publish` workflow -> `pypi`.
- `Dockerfile` + a docker build/push workflow -> `docker`; an `upstream-version.json` tracker -> `upstream-wrapper`.
- `custom_components/*/manifest.json` + `hacs.json` -> `homeassistant`; a codegen workflow -> `codegen`; no `build-*` task -> `source-only`; governance-only -> `docs`.

## 3. Applicability Gate

Reuse [`WORKFLOW.md`][workflow] section 1: a check that governs a construct the repo does not contain is **N/A** - record it as N/A and **exclude it from the verdict**. N/A is never a defect. A Docker check on a repo with no image, a NuGet check on a Python package, the artifact-lifecycle clauses on a source-only repo - all N/A.

## 4. Per-Dimension Checks (Letter and Intent)

For each applicable type in [`spec/project-types.json`][project-types] and every cross-cutting dimension, evaluate each check at its stated verdict tier:

- **letter** - the exact file, section, config, or construct is present.
- **intent** - an equivalent outcome holds even if the form differs.

A check with `intentRef`/`workflowRef` points at the prose section that owns the rationale; read it to judge intent. The dimensions:

- **csharp** - `.editorconfig` carries the shared `[*.cs]` rule block (letter); analyzer severities are enforced, not relaxed (intent).
- **nuget** - publish uses OIDC Trusted Publishing, no `NUGET_API_KEY` (letter+intent); `--skip-duplicate`.
- **pypi** - OIDC publish job with `environment: pypi`, `id-token: write`, `skip-existing: true`; no stored token.
- **python** - ruff and pyright present (intent), canonical in `pyproject.toml` (letter); standalone `.ruff.toml` / `pyrightconfig.json` is a drift finding.
- **console** - smoke runtime matrix is a strict subset; per-runtime outputs aggregate to one `release-asset-*`, gated `!smoke`.
- **docker** - registry layer cache (`buildcache-<branch>`, never `type=gha`); the size-limited Docker Hub README is published via the docker-readme task; the image always re-pushes on publish.
- **branch-model** - `main` and `develop` both exist and are protected; the live rulesets match [`repo-config/*.json`][repo-config] by normalized diff (below).
- **repo-setup** - every required secret for the repo's publish mechanisms is configured, and no forbidden secret is present (per [`spec/secrets.json`][secrets]).
- **linter-parity** - one config per linter (`.markdownlint-cli2.jsonc`, `cspell.json`, ruff/pyright, editorconfig/csharpier, actionlint) drives the editor extension, the CLI, and CI, and CI runs each.
- **recurring-violations** (high priority, always run) - comments concise and non-narrative; ASCII only (no em-dash, no smart quotes); US spelling; line endings per `.editorconfig`. These are frequent regressions; each is a grep-able check (see below).
- **readme-structure** - the README follows [`spec/readme-structure.md`][readme-structure] (applicable sections, in order).

## 5. Assert the Actions Implement WORKFLOW.md

Run [`WORKFLOW.md`][workflow]'s methodology against the repo's **own** Actions: the 5A static audit (structural facts per applicable D-guarantee, each with a `file:line` citation) and the 5B trace scenarios (predicted run/skip + version + release + artifact-end-state vs expected). The contract in WORKFLOW.md section 4 is satisfied by **outcome**, not by matching the catalog snippets in [`catalog/snippets/workflows/`][workflows] byte for byte - those are the reference implementation, not required bytes.

## 6. Validate Rulesets and Secrets

- **Rulesets** - diff each live ruleset against the committed expected payload with a normalized comparison (sort the order-insensitive `rules[]` and `bypass_actors[]` before diffing so a reordered but equivalent ruleset does not read as drift):

  ```sh
  norm='{name,target,enforcement,bypass_actors,conditions,rules} | .rules|=sort_by(.type) | .bypass_actors|=sort_by(.actor_id)'
  for b in develop main; do
    id=$(gh api "repos/<owner>/<repo>/rulesets" --jq ".[]|select(.name==\"$b\").id")
    diff <(jq -S "$norm" "repo-config/$b.json") \
         <(gh api "repos/<owner>/<repo>/rulesets/$id" --jq '{name,target,enforcement,bypass_actors,conditions,rules}' | jq -S "$norm") \
      && echo "$b: in sync" || echo "$b: DRIFT"
  done
  ```

- **Secrets** - confirm each required secret exists (name only; values are not readable). Check the Actions store and, where the mechanism needs it (Docker Hub, codegen App), the Dependabot store too.

## 7. Verdict Model

Per dimension, record `operational | not-operational | N/A`, each with a letter verdict and an intent verdict:

- letter miss but intent satisfied -> **drift finding** (equivalent outcome in a non-standard form; worth fixing, not a break).
- letter and intent both miss -> **defect** (not operational).

A repo is **operational** only if every applicable check passes. A single applicable defect makes it not operational, regardless of how clean the rest looks. N/A items are excluded, never counted as failures.

## 8. Report

Write `reports/<repo>/audit.md` from [`reports/_template.md`][template]: a dimension x {letter, intent, verdict, evidence} table with `file:line` citations (WORKFLOW.md 5A style), a drift section, and a list of proposed registry/spec updates (e.g. a resolved `classificationPending`). Rank findings most severe first.

## 9. Escalate

Surface spec questions rather than resolving them silently - e.g. the Python config-placement canonicalization, or a new construct no type covers. A repeated letter miss that many repos share is a signal the spec (not each repo) needs adjusting; raise it.

<!-- Workflow -->

[workflows]: ./catalog/snippets/workflows/

<!-- Repo -->

[agents]: ./AGENTS.md
[codestyle]: ./CODESTYLE.md
[project-types]: ./spec/project-types.json
[readme-structure]: ./spec/readme-structure.md
[repo-config]: ./repo-config/
[reports]: ./reports/
[repos]: ./registry/repos.json
[secrets]: ./spec/secrets.json
[spec]: ./spec/
[template]: ./reports/_template.md
[workflow]: ./WORKFLOW.md

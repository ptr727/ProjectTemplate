# Repo Scripts

Local checks and tooling this repo runs by hand and in its hook chain. Each one exists because the CI linters pass on the failure it catches: `markdownlint`, `cspell`, `actionlint`, and `editorconfig-checker` all report clean on prose that breaks a documented [`GOVERNANCE.md`](../GOVERNANCE.md) rule.

**Hub-only, not carried.** These are not declared in [`spec/files.json`](../spec/files.json), so the audit does not expect a downstream repo to ship them - the same footing as `spec/audit.py`. Promoting one to fleet-carried is a deliberate act: declare it in the baseline and vendor it, per [`spec/section-model.md`](../spec/section-model.md).

Python only, standard library only, no third-party packages. Every script is read-only and exits non-zero on a finding.

## `prose_lint.py`

Enforces the [`GOVERNANCE.md`](../GOVERNANCE.md) "Documentation Style Conventions" rules that no linter checks: typographic Unicode where an ASCII equivalent exists, a semicolon joining two independent clauses, and a duplicated consecutive word. The two documented non-ASCII exceptions, scientific symbols and developer-typed characters, are deliberately not flagged.

Run it scoped to changed lines, matching the standing rule that existing prose is corrected as each file is next edited rather than swept:

```sh
python3 scripts/prose_lint.py . --diff origin/develop
```

Whole-tree (`python3 scripts/prose_lint.py .`) reports the legacy backlog as well, which is informational rather than a gate.

**Known recall gap:** the splice detector keys on a pronoun or article after the semicolon, so an imperative splice ("Delegate exploration; keep synthesis") is missed. Reading the semicolons in a diff by eye still catches what it cannot.

## `repo_gate.py`

Two deterministic checks:

- `sha-pin` - every workflow `uses:` is a 40-hex commit SHA, with the one documented `dotnet/nbgv@master` exception allowed.
- `eol` - every path pinned LF in [`.gitattributes`](../.gitattributes) has the matching [`.editorconfig`](../.editorconfig) override the line-ending rule requires, with EditorConfig brace syntax expanded.

```sh
python3 scripts/repo_gate.py
python3 scripts/repo_gate.py --check sha-pin
```

A stale-backticked-path check was built and **rejected**: a template repo legitimately references paths that live in downstream repos, so it produced 34 false positives on a clean tree with no way to separate those from real drift. Doc-to-doc drift is a review lens, not a regex.

## `pr_review.py`

One compact digest of a pull request's Copilot review state, replacing a sequence of one-`gh`-call-per-turn polls. `status` prints the digest, and `wait` runs the backoff in-process so a long review wait costs one agent turn instead of one per poll. Read-only by design: the mutations (re-request, reply, resolve) stay as explicit `gh` calls so they remain visible to the `gh-write-guard` hook and to review, and their runbook is in [`.github/copilot-instructions.md`](../.github/copilot-instructions.md).

```sh
python3 scripts/pr_review.py status 452
python3 scripts/pr_review.py wait 452 --timeout 2700
```

`wait` exits `30` when the review is still pending at the timeout, which is pending rather than failed.

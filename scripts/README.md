# Repo Scripts

Local checks and tooling this repo runs by hand, with the deterministic ones also gating CI. Each one exists because the CI linters pass on the failure it catches: `markdownlint`, `cspell`, `actionlint`, and `editorconfig-checker` all report clean on prose that breaks a documented [`GOVERNANCE.md`][governance] rule. Doc linters stay out of the pre-commit hook, which runs language formatting only so it stays fast.

**Hub-only, not carried.** These are not declared in [`spec/files.json`][files], so the audit does not expect a downstream repo to ship them, the same footing as `spec/audit.py`. Promoting one to fleet-carried is a deliberate act: declare it in the baseline and vendor it, per [`spec/section-model.md`][section-model].

Python only, standard library only, no third-party packages. Every script is read-only and exits non-zero on a finding.

Each script has a `test_<script>.py` beside it, driving its gates against input they must reject, because a gate nobody has watched fail is a gate nobody knows works. Where a case covers a table it reads the live table rather than restating it, and each one asserts a floor on what a healthy run reaches, since a check whose scan matches nothing reports zero findings and reads exactly like a pass.

```sh
python3 scripts/test_prose_lint.py
python3 scripts/test_repo_gate.py
python3 scripts/test_pr_review.py
python3 -m unittest discover -s scripts          # all three, and exits 5 if the suite vanishes
uvx coverage@latest run --source=. -m unittest discover -s scripts && uvx coverage@latest report
```

## `prose_lint.py`

Enforces the [`GOVERNANCE.md`][governance] "Documentation Style Conventions" rules that no linter checks: non-ASCII judged against the charset rule's three tiers, a semicolon in prose, a spaced hyphen joining or interrupting a sentence, a duplicated consecutive word, a British spelling, and the shape of a comment's prose.

The tiers decide by context rather than by a flat ban. Tier 1 carries no meaning its ASCII form loses and always flags. Tier 2 is an operator, kept next to a figure or another operator and replaced between words, so a threshold table reads as the range it is. Tier 3 is a unit or scientific symbol whose ASCII form would be a lie and never flags. Developer-typed characters such as emoji are preserved regardless of tier, and an un-tiered one is still reported as `charset-unknown` until it is classified.

A character in no tier is a `charset-unknown` finding rather than a silent pass, since a gate that allows whatever it does not recognize stops gating as the character set grows. Classifying one is a fleet-law edit, so CI surfaces it without blocking on it.

Run it scoped to changed lines, matching the standing rule that existing prose is corrected as each file is next edited rather than swept:

```sh
python3 scripts/prose_lint.py . --diff origin/develop
```

Whole-tree (`python3 scripts/prose_lint.py .`) reports the legacy backlog as well, which is informational rather than a gate. `charset`, `dupword` and `spelling` are clean tree-wide, so CI gates those three and reports the rest warn-only.

The `spelling` rule covers the US English convention where cspell does not reach. That gate reads README and HISTORY only, deliberately, because gating every markdown file would mean endlessly padding `cspell.json` with technical terms, so a British spelling anywhere else in the tree had nothing checking it. The banned words are generated from stems rather than listed one by one, since an inflected spelling is as wrong as its base and a hand-listed family drifts as soon as one form is added without the others. Two words are deliberately absent: `analyses` is the US plural of `analysis` as much as it is a British verb form, and `cancelled` is a GitHub Actions job status rather than prose.

**Outside markdown the rule reads the comments, not the source lines**, reusing the extraction the `comment-wrap` rule already does. An identifier, a string literal, or a lookup table is code, and judging it as prose would make this script report its own table of banned words.

**Scope** is every text file git tracks, binaries skipped by a NUL-byte check, with no extension allowlist: an allowlist covers what its author thought of and silently stops covering whatever is added next, which is the same reason the line-endings rule already requires `git ls-files` over a raw `find`. `--list-files` prints the discovered set for auditing.

A double-quoted span in markdown is treated as a quotation and not scanned for prose rules, so a rule that states its own counter-example does not report the document that documents it. Outside markdown a double quote is structural, so the prose inside it still counts.

The `semicolon` and `dash` rules ban a construction rather than a detectable subset of it, so each flags by default and the exceptions are the ones the rule names: a semicolon inside a list that already carries commas, and for the dash a compound word, a leading list marker, a range, and the `- **Label** - explanation` separator that opens a governed bullet.

**Both are markdown-only for now.** A shell script carries 78 statement separators that are not prose at all, so telling a comment from code is a precondition for reaching source files. Until then a semicolon or dash in a code comment is missed, which reading the diff by eye still catches.

The `comment-wrap` rule covers comments in every syntax the fleet's project types carry, not only the hash ones: `//` and `/* */` for C#, C, C++ and JSONC, `/* */` alone for CSS, `<!-- -->` for XML, csproj and markdown, `<# #>` for PowerShell, `;` for INI, and `#` for Python, shell, YAML and TOML.

JSON is treated as JSONC, because that is what ships: VS Code tasks, launch, devcontainer and workspace files all carry comments under a plain `.json` name. A marker inside a string literal is not a comment, so each line is scanned with quoted spans blanked first, and Python uses `tokenize` so a trailing comment is seen exactly. A documentation comment (`///`, `/**`, a docstring) is left to CODESTYLE, which permits the paragraphs this rule forbids.

Each syntax also declares how its strings escape: the escape character, the quotes it works inside, and whether it works outside one, all read independently of whether a string embeds its delimiter by doubling it. Neither property implies the other: PowerShell's double-quoted string is escaped by a backtick and doubling at once, while a C# verbatim string is doubling and not escaped. Reading an escape a string does not have consumes its closing quote and blanks the rest of the line, and missing one it does have ends the string early on the escaped quote.

A string that spans lines carries its state onto the lines it covers, so a marker inside one is data rather than a comment. Each syntax declares the forms it carries: the C# verbatim string, an ordinary quoted string in shell and in PowerShell, a PowerShell here-string, a shell heredoc, and a YAML block scalar. A YAML `run:` scalar is deliberately not one of them, because it holds a script whose `#` lines are exactly the comments this rule governs. A form no syntax declares stays scanned a line at a time, which is where a false positive is still possible: a TOML triple-quoted string is the open case. The reverse direction is guarded too, since a form that carried where the language has none would blank the rest of the file and report nothing: a YAML plain scalar's apostrophe is not a string, so an ordinary quote does not carry there.

A comment sentence also has to start with a capital, which `comment-case` checks. A lowercase opening reads as the continuation of the line above it, so the two rules are read together: a wrapped sentence reports as `comment-wrap`, and a lowercase opening that is not a continuation reports as `comment-case`. Where the first word is a tool whose own casing is lowercase, the fix is to restructure rather than to capitalize the name against CODESTYLE's tooling-casing rule.

`charset` and `dupword` are clean tree-wide and gate CI. `charset-unknown`, `semicolon`, `dash`, `comment-wrap`, and `comment-case` run as one warn-only CI step, so the backlog is visible without blocking and is corrected as each file is next edited.

## `repo_gate.py`

Two deterministic checks:

- `sha-pin`: every workflow `uses:` is a 40-hex commit SHA, with the one documented `dotnet/nbgv@master` exception allowed.
- `eol`: every path pinned LF in [`.gitattributes`][gitattributes] has the matching [`.editorconfig`][editorconfig] override the line-ending rule requires, with EditorConfig brace syntax expanded. One direction only: an `.editorconfig` LF glob with no git pin is legitimate, since `.editorconfig` governs what the editor writes where git enforces a class it must not guess at.

```sh
python3 scripts/repo_gate.py
python3 scripts/repo_gate.py --check sha-pin
```

A stale-backticked-path check was built and **rejected**: a template repo legitimately references paths that live in downstream repos, so it produced 34 false positives on a clean tree with no way to separate those from real drift. Doc-to-doc drift is a review lens, not a regex.

## `pr_review.py`

One compact digest of a pull request's Copilot review state, replacing a sequence of one-`gh`-call-per-turn polls. `status` prints the digest, and `wait` runs the backoff in-process so a long review wait costs one agent turn instead of one per poll. Read-only by design: the mutations (re-request, reply, resolve) stay as explicit `gh` calls so they remain visible to the `gh-write-guard` hook and to review, and their runbook is in [`.github/copilot-instructions.md`][copilot-instructions].

```sh
python3 scripts/pr_review.py status 452
python3 scripts/pr_review.py wait 452 --timeout 2700
```

`wait` exits `30` when the review is still pending at the timeout, which is pending rather than failed. Its failure mode is a wrong answer rather than a crash, so the cases feed crafted GraphQL payloads: a review attributed to the wrong login, a review counted against a stale head, a maintainer's own thread read as a finding, and a wait that returns success while nothing landed. One case reads the reviewer login out of the runbook rather than restating it, since GraphQL drops the `[bot]` suffix REST carries, and another asserts no mutation has crept into a read-only script.

The digest also reports the **suppressed findings** a review body collapses into a `<details>` block. Those reach no review thread, so a loop that polls threads alone reports a clean pass while they stand, and the [merge gate][governance] counts them as outstanding findings either way. `suppressed=N` counts findings rather than blocks, reading the `(N)` the heading carries, since one body holds one block per round and counting blocks reports two findings as one. It covers the reviews on the current head only, so a finding answered before a push does not re-open after it. Each block prints whole where a thread body truncates, because a thread can be re-read at its id and a suppressed finding cannot, and it prints under a marker naming what closing it takes: no thread exists to reply on or resolve, so the answer goes in the PR conversation.

The match is on the block's heading rather than anywhere in the body, and on the runbook's alternation rather than on one phrasing, since the wording has already appeared two ways. A case asserts the script's pattern is the one the runbook publishes rather than a copy of it that can drift. Reading the whole body was the first implementation and its own review caught it: a review whose overview prose discusses suppressed findings carries none, and reporting that as a finding trains the reader to skim the field. A heading outside any `<details>` wrapper is still read, because reporting zero when the markup moves is the same false clean one level up, and that fallback takes a count so ordinary prose does not become one.

<!-- Internal -->

[copilot-instructions]: ../.github/copilot-instructions.md
[editorconfig]: ../.editorconfig
[files]: ../spec/files.json
[gitattributes]: ../.gitattributes
[governance]: ../GOVERNANCE.md
[section-model]: ../spec/section-model.md

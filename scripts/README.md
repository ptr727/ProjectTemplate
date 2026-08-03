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

The default rule set covers comment shape (`comment-wrap` and `comment-case`) alongside the prose rules. It did not, which meant a run nobody parameterized reported clean on a wrapped comment while the rule read as enforced, and comment shape is the most frequently regressed rule in agent-authored work. Reading the backlog it exposes needs no flag now, and gating it still needs `--diff`, because the tree carries several hundred of them.

A wide scan skips the trees this repo generates rather than authors, currently `reports/`, which [`spec/audit.py`][audit] writes. A finding there is the audit engine's phrasing rather than an author's, so no edit to that tree can fix it, and leaving them in made the repo's own number mostly generated output. Naming such a path directly still reads it (`prose_lint.py reports`), so nothing becomes uncheckable.

In markdown an HTML comment carrying no sentence punctuation is treated as a structural marker rather than commentary, so it takes neither a capital nor a sentence split. The reference-link group headers, the ToC-omit directive, and the `agent-safety` install markers are each matched verbatim by a tool, so rewriting one to satisfy the rule breaks whatever reads it. A markdown comment that does punctuate a sentence is prose and is judged as prose.

The `spelling` rule covers the US English convention where cspell does not reach. That gate reads README and HISTORY only, deliberately, because gating every markdown file would mean endlessly padding `cspell.json` with technical terms, so a British spelling anywhere else in the tree had nothing checking it. The banned words are generated from stems rather than listed one by one, since an inflected spelling is as wrong as its base and a hand-listed family drifts as soon as one form is added without the others. Two words are deliberately absent: `analyses` is the US plural of `analysis` as much as it is a British verb form, and `cancelled` is a GitHub Actions job status rather than prose.

**Outside markdown `spelling` and `dupword` read the comments, not the source lines**, reusing the extraction the `comment-wrap` rule already does. An identifier, a string literal, or a lookup table is code, and judging it as prose would make this script report its own table of banned words. Each comment on a line is judged on its own rather than joined with its neighbors, because two comments are two sentences and joining them reads the second's opening word as a repeat of the first's last.

`dupword` gates CI, so its scope decides what a correct file is allowed to contain. A repeated token outside a comment is usually correct authoring rather than a typo: `class="gallery gallery-cols-1"` is the ordinary way two CSS class names share a prefix, and `rel`, `srcset`, `sizes` and the `data-*` attributes all take value lists of the same shape. There is no edit that satisfies the rule without changing the rendered page, so a blocking gate that reads those lines rejects correct work. The cost of the narrower scope is stated plainly rather than hidden: a duplicated word in HTML body text, or in a YAML or JSON string value, is no longer caught. Narrowing to the comment is preferred over exempting an attribute, since an exemption list covers only the attributes its author thought of.

**Scope** is every text file git tracks, binaries skipped by a NUL-byte check, with no extension allowlist: an allowlist covers what its author thought of and silently stops covering whatever is added next, which is the same reason the line-endings rule already requires `git ls-files` over a raw `find`. `--list-files` prints the discovered set for auditing.

A double-quoted span in markdown is treated as a quotation and not scanned for prose rules, so a rule that states its own counter-example does not report the document that documents it. Outside markdown a double quote is structural, so the prose inside it still counts.

The `semicolon` and `dash` rules ban a construction rather than a detectable subset of it, so each flags by default and the exceptions are the ones the rule names: a semicolon inside a list that already carries commas, and for the dash a compound word, a leading list marker, a range, and the `- **Label** - explanation` separator that opens a governed bullet.

**The semicolon rule reads the list where it lives.** The comma qualifies the list as a whole rather than one separator's position, so an enumeration whose commas fall in a later item keeps every semicolon it carries. Reading it positionally split one series in two, flagging the openers of the same list it then exempted the tail of, which would have restructured the enumerated guarantees the exemption exists to protect. A markdown table row is judged one cell at a time, since a row is a record of fields and a comma in one column cannot excuse a semicolon in another, and a bullet's `**Label**:` is dropped before the line is read, because it opens the bullet rather than announcing a list, the same construct the label dash is exempted for. What this misses is a sentence that reads as a list without being one: a colon early in a long line still excuses a splice later on it, which reading the diff catches.

**Both are markdown-only for now.** A shell script carries 78 statement separators that are not prose at all, so telling a comment from code is a precondition for reaching source files. Until then a semicolon or dash in a code comment is missed, which reading the diff by eye still catches.

The `comment-wrap` rule covers comments in every syntax the fleet's project types carry, not only the hash ones: `//` and `/* */` for C#, C, C++ and JSONC, `/* */` alone for CSS, `<!-- -->` for XML, csproj and markdown, `<# #>` for PowerShell, `;` for INI, and `#` for Python, shell, YAML and TOML.

JSON is treated as JSONC, because that is what ships: VS Code tasks, launch, devcontainer and workspace files all carry comments under a plain `.json` name. A marker inside a string literal is not a comment, so each line is scanned with quoted spans blanked first, and Python uses `tokenize` so a trailing comment is seen exactly. A documentation comment (`///`, `/**`, a docstring) is left to CODESTYLE, which permits the paragraphs this rule forbids.

Each syntax also declares how its strings escape: the escape character, the quotes it works inside, and whether it works outside one, all read independently of whether a string embeds its delimiter by doubling it. Neither property implies the other: PowerShell's double-quoted string is escaped by a backtick and doubling at once, while a C# verbatim string is doubling and not escaped. Reading an escape a string does not have consumes its closing quote and blanks the rest of the line, and missing one it does have ends the string early on the escaped quote.

A string that spans lines carries its state onto the lines it covers, so a marker inside one is data rather than a comment. Each syntax declares the forms it carries: the C# verbatim string, an ordinary quoted string in shell and in PowerShell, a PowerShell here-string, a shell heredoc, and a YAML block scalar. A YAML `run:` scalar is deliberately not one of them, because it holds a script whose `#` lines are exactly the comments this rule governs. A form no syntax declares stays scanned a line at a time, which is where a false positive is still possible: a TOML triple-quoted string is the open case. The reverse direction is guarded too, since a form that carried where the language has none would blank the rest of the file and report nothing: a YAML plain scalar's apostrophe is not a string, so an ordinary quote does not carry there.

A comment sentence also has to start with a capital, which `comment-case` checks. A lowercase opening reads as the continuation of the line above it, so the two rules are read together: a wrapped sentence reports as `comment-wrap`, and a lowercase opening that is not a continuation reports as `comment-case`. Where the first word is a tool whose own casing is lowercase, the fix is to restructure rather than to capitalize the name against CODESTYLE's tooling-casing rule.

**A comment whose whole body is a URI is a reference rather than a sentence**, and neither rule applies to it. It cannot be capitalized or restructured without corrupting the address it exists to carry, so before the exemption every repo carrying a reference block inherited a finding no edit could answer. Consecutive reference lines are separate addresses rather than one sentence wrapping, which is why the exemption also stops the line below a URI from reading as its continuation. A URI inside a sentence is still prose, so the exemption requires the whole body to be the address and nothing else.

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

`wait` exits `40` when Copilot answers the request with a plain comment rather than a review, meaning a comment of its own that postdates its newest review on the pull request. The test is the **shape** of that answer and not its cause, which the script reads nothing of: a comment carries no commit, so it satisfies no coverage check whatever it says, and a wait reading formal reviews alone treats it as an unmet condition and then polls out its whole timeout against an answer that already arrived. A refusal is the case that makes this worth catching, a quota or rate-limit message among them, and `40` neither asserts nor detects one. The comment prints whole because its wording is the only thing separating a refusal, which is terminal since no review follows it and re-requesting does not clear it, from an ordinary remark that is not, so `40` ends the wait and hands the text to the reader who can tell them apart. A comment **older** than the newest review is spent rather than terminal, because the review it preceded did land. Every connection reads the newest `WINDOW` nodes rather than the reviewer's own, since GraphQL offers no author filter, so ordinary traffic is what pushes theirs out of reach. `window_blind` is the one guard over both sides, and each side fails differently. Blind on **comments** means an answer could be back there unseen, which reads as `answered_outside_review=unknown` rather than `no`. Blind on **reviews** is worse, because the newest review in view is then not the newest there is, and an empty baseline dates every comment as newer so each one reads as an answer: a false `40` that stops the loop on a pull request whose review actually landed. That case reports nothing and lets the wait keep polling, since a wait that runs on is visible where a wrong terminal is not.

Everything else is decidable and says so. One of the reviewer's own nodes in view, even a **spent** one, settles the question, because nodes arrive in creation order, so anything behind the window is older than everything inside it. A window holding every node the pull request has is settled too, which is why the guard reads `pageInfo.hasPreviousPage` rather than the node count: a full window and a complete one are the same length, so length alone would report a gap where none exists. Cases hold `WINDOW` equal across all four windows and hold all four to asking for `hasPreviousPage`, since a connection that stops asking reports `no` instead of `unknown`, the silent narrowing one level up. The timeout path prints the full digest for the same reason, as a bare `PENDING` line reports a slow reviewer and a broken poll identically, which is the reading that turns a stalled watcher into a watcher nobody notices is stalled.

The digest also reports the **suppressed findings** a review body collapses into a `<details>` block. Those reach no review thread, so a loop that polls threads alone reports a clean pass while they stand, and the [merge gate][governance] counts them as outstanding findings either way. `suppressed=N` counts findings rather than blocks, reading the `(N)` the heading carries, since one body holds one block per round and counting blocks reports two findings as one. It covers **every** round rather than the current head, because a suppressed finding has no resolved state for a push to retire: head-scoping read "superseded by a push" as "answered", and a finding nobody replied to left the digest the moment the branch moved, so the run reported zero. That is how four rounds went unanswered across three pull requests in one day, each found by the maintainer rather than by this script. The summary line splits the count as `suppressed=N (on_head=N earlier=N)` and each block is marked with the round that raised it, since a finding on an older round may since be moot and deciding that is the reader's call rather than one the count should make for them. Each block prints whole where a thread body truncates, because a thread can be re-read at its id and a suppressed finding cannot, and it prints under a marker naming what closing it takes: no thread exists to reply on or resolve, so the answer goes in the PR conversation.

The match is on the block's heading rather than anywhere in the body, and on the runbook's alternation rather than on one phrasing, since the wording has already appeared two ways. A case asserts the script's pattern is the one the runbook publishes rather than a copy of it that can drift. Reading the whole body was the first implementation and its own review caught it: a review whose overview prose discusses suppressed findings carries none, and reporting that as a finding trains the reader to skim the field. A heading outside any `<details>` wrapper is still read, because reporting zero when the markup moves is the same false clean one level up, and that fallback takes a count so ordinary prose does not become one.

<!-- Internal -->

[audit]: ../spec/audit.py
[copilot-instructions]: ../.github/copilot-instructions.md
[editorconfig]: ../.editorconfig
[files]: ../spec/files.json
[gitattributes]: ../.gitattributes
[governance]: ../GOVERNANCE.md
[section-model]: ../spec/section-model.md

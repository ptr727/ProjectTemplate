# Line Ending Policy

Full detail for the "Line endings" rule in `SKILL.md`. Load this when choosing an ending for a
new file type, working in an operational (config) repo, pinning an extensionless executable, or
auditing a repo's endings, not for an ordinary content edit to an existing file (the SKILL.md
summary, preserve the existing ending and verify with a byte scan, covers that case).

## The defaults

- **[`.editorconfig`](../../../../.editorconfig) sets the line ending.** `[*] end_of_line = crlf`
  is the default, every file type is CRLF unless pinned otherwise, with LF pinned for the
  execution-sensitive exceptions: `*.sh`, Dockerfiles, and any individual `.py` executed directly
  via its shebang (pinned by path, for example `spec/validate.py`, vanilla `.py` stays CRLF, since
  Python's universal newlines accept it and it is commonly edited on Windows). Only the LF
  exceptions are declared, the redundant per-type CRLF rules are intentionally omitted.
- **`.gitattributes` mirrors it**: `* -text` (git stores the exact bytes committed and does not
  normalize) plus the matching LF pins.
- **Both files are required together.** `.editorconfig` governs the editor, `.gitattributes`
  governs git (checkout, commit, `--renormalize`). A repo missing either file, or whose
  `.editorconfig` sets no global `end_of_line` default (for example declares it only under
  `[*.md]`), accumulates files mixed between LF and CRLF, the exact failure these two files
  prevent together. Carry both files whole (an inert `[*.cs]` block costs nothing in a non-.NET
  repo), including the `*.sh text eol=lf` pin and any extensionless-script path pins.

## Choosing an ending for a new file type

CRLF is the default, since cross-platform editors on Windows produce it and it is harmless on
Linux for everything except shell. Use LF only when the type requires it or CRLF breaks how it is
consumed: executable scripts and shebangs (`*.sh`, s6, husky), Dockerfiles (CRLF breaks `RUN`
heredocs and line continuations), and tool-owned formats with a native LF ending (KiCad).
Non-workflow YAML stays CRLF, GitHub Actions' parser tolerates it (a repo also running yamllint
sets `new-lines: disable` to defer to `.editorconfig`). Workflow YAML
(`.github/workflows/*.{yml,yaml}`) is pinned LF in `.editorconfig`, because Dependabot and Actions
rewrite it with LF, so declaring LF keeps it consistent instead of mixed on every bump. This LF
class is not backed by a `.gitattributes` pin, git keeps `* -text`, and CI's `editorconfig-checker`
(EOL-only) catches a mismatch instead.

Distinguish where a file is *consumed* from where it is *edited*, consumption on Linux alone does
not force LF. A config or pattern file consumed by a Linux tool stays CRLF when the tool tolerates
a trailing CR: `.dockerignore` and `.gitignore` are CRLF (their parsers strip the CR), and only a
*Dockerfile*, interpreted and broken by a CR in a `RUN` heredoc or line continuation, is LF.

## Operational (config) repos

The global default follows the consuming application's native platform, not the fleet CRLF
default. A config repo (registry `workflowModel: operational`) is a view into an application's
configuration directory, often the exact tree mounted into that app's container, so its files use
the ending the app itself reads and writes, and forcing the fleet CRLF default would fight the
app. Set the `[*] end_of_line` default to the app's native ending and record it in the registry
`lineEndings` field (`lf` or `crlf`): LF for a Linux-native app whose config lives in a Linux
container (ESPHome, Home Assistant, a devcontainer-only or HACS config), CRLF for a Windows-native
editor, for example Vantage InFusion config edited by Design Center on Windows. The
execution-sensitive LF pins (`*.sh`, Dockerfiles, workflow YAML) still apply on top, and
`.gitattributes` still mirrors the chosen default. This override is for operational repos only,
`release` repos keep the `[*] end_of_line = crlf` fleet default above. Do not re-normalize an
operational repo to the fleet default, that is exactly the over-normalization these per-repo
endings exist to prevent.

**Mixed-consumer config: prefer to split by platform into single-platform repos, not one mixed
repo.** When a config repo would be consumed on two platforms (a Linux app plus a Windows-edited
subtree), the clean answer is a repo per consumer, each single-platform with its own
`lineEndings`. For example a controller config edited by a Windows-native editor lives in its own
CRLF repo, not as a subtree inside a Linux `lf` config repo. Fallback only if a subtree genuinely
cannot be split out: keep the global default at the primary consumer and pin the odd subtree with
an `.editorconfig` path override (for example `[<subtree>/**] end_of_line = crlf`) matching its
consumer. The global `* -text` in `.gitattributes` already preserves those bytes, no extra git pin
is needed.

## Scripts and extensionless executables

Must be LF, and pinned in `.gitattributes`, not just configured. A CRLF shebang
(`#!/usr/bin/env bash\r`) breaks execution. `.editorconfig` sets `[*.sh] = lf`, but that
extension-based rule does not match extensionless executables (s6 service scripts `run`/`up`/
`finish`, husky or git hook scripts like `.husky/pre-commit`), and `* -text` enforces nothing, so
a broad normalization pass or an editor can silently flip them to CRLF. `.gitattributes` is the
enforcement layer: it carries `*.sh text eol=lf`, and any repo whose tooling ships extensionless
scripts adds the matching path pin (`Docker/s6-overlay/** text eol=lf` for s6 init,
`.husky/pre-commit text eol=lf` for husky hooks), so git holds them at LF on checkout and
`--renormalize`. This pin is mandatory for any repo that overrides s6 init, uses husky or git
hooks, or otherwise ships executable scripts. The same explicit-pin rule extends to tool-owned
file formats the base config does not key on: pin them to whatever ending the tool reads and
writes, for example KiCad project, footprint, and 3D files (`*.kicad_mod`, `*.kicad_sym`,
`*.step`), which KiCad writes LF.

**Pair each such pin with a matching `.editorconfig` override**, since the git pin alone is not
enough, `.gitattributes` governs git while the editor follows `.editorconfig`, where the default
still applies to any file no extension rule covers. Give every extensionless executable an
editorconfig LF override beside its `.gitattributes` pin (`[.husky/pre-commit] end_of_line = lf`),
and for a byte-preserve data directory (downloaded or opaque source whose exact bytes the consumer
may depend on) disable all editor normalization, not just EOL: `[<dir>/*]` with `charset = unset`,
`end_of_line = unset`, `insert_final_newline = false`, `trim_trailing_whitespace = false` (`unset`
is EditorConfig's spec-defined special value that removes an inherited property).

## Editing discipline

- **New files**: create with the `.editorconfig`-mandated ending.
- **Editing an existing file**: preserve its current line endings, do not reflow them as a side
  effect of a content change, even if the file is already non-compliant. A tool that rewrites a
  file in text mode (a script, a bulk find/replace) can silently flip CRLF to LF and turn a
  one-line change into a whole-file diff. After any programmatic edit, verify before staging:
  `git diff --stat` should touch only the lines you changed, and a byte check should confirm the
  expected ending. If a diff balloons to the whole file, the endings flipped, restore them and
  re-stage.
- **Fixing a non-compliant file**: bring it to its `.editorconfig` ending as a deliberate change,
  and prefer to isolate it in its own EOL-only commit so the churn is reviewable. When a broader
  maintenance change has to normalize endings alongside content edits, call it out explicitly in
  the commit or PR description and verify the content separately with
  `git diff --ignore-cr-at-eol`.

## Auditing

Don't trust `file` or a naive `git ls-files --eol`. The authoritative check is a byte scan that
classifies by which endings are present: CRLF-only (every `\n` preceded by `\r`), LF-only (no
`\r`), or mixed (both forms present). Flag mixed explicitly rather than lumping it in with CRLF,
and skip binaries via a NUL-byte check. `file` mislabels some types (it reports a CRLF `.json` or
`.code-workspace` as plain "JSON text data" with no CRLF note), and `git ls-files --eol`'s `attr/`
column holds multiple tokens that shift naive field-splitting into false positives. Scope a
repo-wide audit to `git ls-files` plus `git ls-files --others --exclude-standard`, never a raw
`find`, which sweeps self-ignoring caches (`.mypy_cache`, `.artifacts`).

Idempotent normalize: `b.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")`. A single within-line
string replace is EOL-safe, but a tool that inserts multiple lines or writes a new file into a
CRLF file must emit `\r\n`, since a naive `\n` insert creates mixed endings. `.code-workspace` is
JSONC (it has `//` comments), so strip them before JSON-parsing it.

Editing CRLF files programmatically with a regex has a sharper trap: `.` matches `\r`, so a
captured line keeps its carriage return and rejoining with `\r\n` yields `CRCRLF`. A text-mode
rewrite has the mirror failure, silently flattening CRLF to LF. Prefer line-based edits
(`splitlines(keepends=True)`) or literal replacement over regex reassembly. In Python the
text-mode failure is the default: `Path.read_text()` decodes through universal newlines and
`write_text()` writes `\n` back, so a read-edit-write round trip flattens the whole file while the
edit itself looks correct. Pass `newline=''` to both, or work in bytes.

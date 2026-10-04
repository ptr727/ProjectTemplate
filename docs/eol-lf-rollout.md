# Fleet LF Rollout

Tracks the fleet-wide line-ending default flip (CRLF to LF) repo by repo. The policy itself lives
in [GOVERNANCE.md "Line Endings"][governance-line-endings] and the `comment-and-doc-style` Skill's
[`references/line-endings.md`][line-endings]. This doc is the rollout checklist only, not a
restatement of the rule. It is **hub-only** and is not carried downstream, the same way
[`docs/fleet-map.md`][fleet-map] is hub-only, because it tracks the hub's own migration rather
than a fact a downstream repo's own docs need to carry.

**Maintenance rule.** The conversion pull request lands in the converted repo, so check its box
here in a hub pull request once that conversion reaches `main`. Update the row where the conversion
found something the summary below did not anticipate, such as a file that has to stay CRLF beyond
the ones already named. A row left unchecked after its conversion reached `main` is stale prose, so this
doc is only trustworthy while that rule holds.

## What Changed

The fleet default flipped from `[*] end_of_line = crlf` to `[*] end_of_line = lf` in
`.editorconfig`, and `.gitattributes` replaced its `* -text` default, under which Git converts
nothing, with `* text=auto eol=lf`. The only CRLF exception is `*.bat` / `*.cmd`, pinned in both
files, the one type Windows itself requires it for. The per-type LF pins the old files carried are
dropped, since the `[*]` default already covers them. The hub (`ProjectTemplate`) carried the
`.editorconfig` flip and a one-time renormalization of every tracked file it touches in the pull
request that added this doc, and the `.gitattributes` default in a later one.

## Per-Repo Conversion

A repo needs this conversion when any of these holds on the branch its work is based on, `develop`
where it keeps one and `main` otherwise:

- `.editorconfig` or `.gitattributes` is missing.
- `.editorconfig` sets no `[*] end_of_line = lf`.
- `.gitattributes` sets no `* text=auto eol=lf`.
- `git ls-files --eol` lists a file at `i/crlf` or `i/mixed` whose attributes are neither `-text`
  nor `binary`, since a file committed with CRLF stays CRLF in the index until it is renormalized,
  whatever the two config files say.

An operational repo whose `registry/repos.json` entry records `lineEndings: crlf` is the exception,
covered at the end of this section.

The conversion lands before any carried instruction file or baseline file. A resync or standup
that carries LF files into a repo still on the CRLF default fails `editorconfig-checker` on every
one of them, and converting each carried file to CRLF to pass is churn the conversion then
reverts. `RESYNC.md` section 3 and `STANDUP.md` section 1A order it first for that reason.

The conversion pull request carries the renormalization, `.editorconfig`, and `.gitattributes`,
and nothing else. A `develop` ruleset that allows only squash merges collapses the pull request to
one commit, so the commit boundaries below are for review before it merges, and the isolation that
survives the squash is the pull request's own file scope. Open it through the repo's normal branching model,
per the [`branching-and-release-model`][branching-and-release-model] Skill, and work in this order:

1. **Merge the hub's defaults into `.editorconfig` and `.gitattributes`, never overwrite either
   file.** Remove the old fleet defaults, `[*] end_of_line = crlf` and `* -text`, rather than
   leaving them beside the new ones, and remove the old fleet's per-type LF pins too, such as
   `*.sh`, `uv.lock`, Dockerfiles, and its by-path script pins, since the new `[*]` default covers
   them. Put the hub's `* text=auto eol=lf` line first in `.gitattributes` and its `[*]` section
   first in `.editorconfig`, then the `*.bat` / `*.cmd` CRLF pins, then every attribute and section
   that is the repo's own, such as LFS filters, `linguist-generated`, `export-ignore`, and the
   repo's own path pins, and then the step 2 pins. In both files a later match overrides a property
   an earlier one set, so a pin above the default is silently cancelled, and a `* -text` below it
   unsets `text` for every file.
2. **Pin every vendor file whose bytes the repo has to keep, before renormalizing.** `text=auto`
   leaves a file byte-preserved only where Git's heuristic detects it as binary, so a text-shaped
   vendor download, a STEP model or DXF and SVG artwork for example, is normalized unless pinned.
   List the candidates with `git ls-files --eol` and pin each type as a pair:
   - `-text` in `.gitattributes`, or `binary` where diffs should be skipped too.
   - An `.editorconfig` section setting `charset`, `end_of_line`, and `insert_final_newline` to
     `unset` and `trim_trailing_whitespace` to `false`, per
     [`references/line-endings.md`][line-endings] "Scripts and extensionless executables".
   - Globs written for both letter cases, such as `*.[sS][tT][pP]`, since attribute matching is
     case-sensitive on a Linux clone.

   A type that has to stay CRLF takes a matching CRLF pin in both files, never one alone, the way
   `Vantage-Config` pins its `*.dc` exports.
3. **Renormalize in its own commit.** Run `git add --renormalize .` after the two config files are
   committed. Only `--renormalize`, or the add of a file new to the index, normalizes, and a plain
   re-add of a file already committed with CRLF leaves it unchanged. Before committing, confirm
   that `git diff --cached --ignore-cr-at-eol` shows no content change and that
   `git diff --cached --stat` lists no file a `-text` or `binary` pin covers, per
   [`references/line-endings.md`][line-endings] "Editing discipline".
4. **Check the branch out fresh, then run `editorconfig-checker` clean.** `--renormalize` changes
   only the index, so the working tree it ran in keeps its CRLF bytes, and `editorconfig-checker`
   reads the working tree. A detached worktree of the branch, `git worktree add --detach <path> <branch>`, holds the
   converted bytes. Then open the
   pull request.

Once the conversion reaches `main`, check the box below and reconcile the repo's `registry/repos.json` entry per
[GOVERNANCE.md "Repository Onboarding and Conformance"][governance-onboarding] where the
conversion surfaced anything the registry did not already record.

For an operational repo whose `lineEndings` is `crlf`: no conversion, since its global default
follows its consuming Windows-native app rather than the fleet default, per
[`references/line-endings.md`][line-endings] "Operational (config) repos", and its box below is
checked as **not applicable** rather than as converted. None currently do: `Vantage-Config` was
the one operational repo recording `crlf`, and it has since converted to the fleet `lf` default
with only its Design Center `.dc` exports pinned CRLF (ptr727/Vantage-Config#28), so its box
below is checked as converted rather than not applicable.

## Rollout Checklist

Repos and their current `registry/repos.json` `workflowModel` / `lineEndings`, from the hub's own
registry as of this doc's authorship, except where an entry records a later reclassification. A
row marked **defaults in place** was read on `main` on 2026-10-03 and had both
`[*] end_of_line = lf` in `.editorconfig` and `* text=auto eol=lf` in `.gitattributes`, and no
committed text file holding CRLF outside a `-text` pin. That is the state a conversion leaves, so
the row is checked without a conversion pull request of its own.

- [x] **ProjectTemplate** (`release`): hub, converted in the pull request that added this doc
- [x] **Utilities** (`release`): defaults in place
- [x] **LanguageTags** (`release`): defaults in place
- [x] **aiopurpleair** (`release`): defaults in place
- [x] **homeassistant-purpleair** (`release`): defaults in place
- [x] **Financial-Modeling** (`release`): defaults in place
- [x] **PlexCleaner** (`release`): defaults in place
- [x] **ESPHome-NonRoot** (`release`): defaults in place
- [x] **VSCode-Server-DotNetCore** (`release`): defaults in place
- [x] **NxWitness** (`release`): defaults in place
- [x] **HomeAutomation-Config** (`release` since its 2026-09-24 reclassification from
      `operational`, `lineEndings: lf`): defaults in place
- [x] **KiCadLibrary** (`release`): converted in ptr727/KiCadLibrary#57, with its vendor STEP,
      DXF, and SVG files pinned `-text` beside a matching `.editorconfig` `unset` section, and PDF
      and PNG pinned `binary`
- [ ] **EspDinIoT** (`release`): `develop` still sets `[*] end_of_line = crlf` and `* -text`,
      and `main` carries neither file
- [x] **ESPHome-Config** (`operational`, `lineEndings: lf`): defaults in place
- [x] **HomeAssistant-Config** (`operational`, `lineEndings: lf`): defaults in place
- [ ] **DevKitCIoT** (`release`): `main` still sets `[*] end_of_line = crlf` and `* -text`, and
      `develop` carries neither file
- [x] **PhotoCleaner** (`release`): defaults in place
- [x] **MediaTools** (`release`): defaults in place
- [ ] **AudioCleaner** (`release`): `.editorconfig` sets no `[*]` `end_of_line`, and
      `.gitattributes` still sets `* -text`
- [x] **Vantage-Config** (`operational`, `lineEndings: lf` since its ptr727/Vantage-Config#28
      conversion): converted outside this rollout, with only its Design Center `.dc` exports
      pinned CRLF, not the "not applicable" case above
- [ ] **HolidayLights** (`release`): `.editorconfig` sets `[*] end_of_line = lf`, but
      `.gitattributes` still sets `* -text`
- [x] **Blog** (`release`, `lineEndings: lf`): defaults in place

<!-- Repo -->

[fleet-map]: ./fleet-map.md
[governance-line-endings]: ../GOVERNANCE.md#line-endings
[governance-onboarding]: ../GOVERNANCE.md#repository-onboarding-and-conformance
[line-endings]: ../.agents/skills/comment-and-doc-style/references/line-endings.md
[branching-and-release-model]: ../.agents/skills/branching-and-release-model/SKILL.md

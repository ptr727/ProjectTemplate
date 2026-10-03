# Fleet LF Rollout

Tracks the fleet-wide line-ending default flip (CRLF to LF) repo by repo. The policy itself lives
in [GOVERNANCE.md "Line Endings"][governance-line-endings] and the `comment-and-doc-style` Skill's
[`references/line-endings.md`][line-endings]. This doc is the rollout checklist only, not a
restatement of the rule. It is **hub-only** and is not carried downstream, the same way
[`docs/fleet-map.md`][fleet-map] is hub-only, because it tracks the hub's own migration rather
than a fact a downstream repo's own docs need to carry.

**Maintenance rule.** Check a repo's box in the same pull request that converts it, and update its
row if the conversion found something the summary below didn't anticipate (a file that genuinely
needs to stay CRLF beyond the ones already named). A register showing a repo unchecked after its
conversion PR merged is itself stale prose, so this doc is only trustworthy while that rule holds.

## What Changed

The fleet default flips from `[*] end_of_line = crlf` to `[*] end_of_line = lf` in
`.editorconfig`. The only CRLF exception going forward is `*.bat` / `*.cmd`, the one type Windows
itself requires it for. `.gitattributes` keeps its existing execution-sensitive LF pins (`*.sh`,
Dockerfiles, `uv.lock`, the shebang-executed `.py` by-path list) unchanged, now redundant with the
new default but retained as git-level enforcement independent of the editor. The hub
(`ProjectTemplate`) carried this change, including a one-time renormalization of every tracked
file the new default touches, in the pull request that added this doc.

## Per-Repo Conversion

For a `release` repo, or an operational repo whose `lineEndings` is already `lf`: pull the hub's
new `.editorconfig`, confirm `.gitattributes` needs no change for the fleet default itself (it
doesn't, per the policy above), renormalize every tracked file the new default now covers from
CRLF to LF (skip anything with a genuine reason to stay CRLF, and give each file type that
genuinely needs a CRLF pin a matching entry in both `.editorconfig` and `.gitattributes`, never
one alone, per `Vantage-Config`'s own `*.dc` pin below and
[`references/line-endings.md`][line-endings] "Scripts and extensionless executables"), run
`editorconfig-checker` clean, and open the PR through the repo's normal branching model
([`branching-and-release-model`][branching-and-release-model] Skill). Isolate the
renormalization from any content edit in its own commit, verified with
`git diff --ignore-cr-at-eol`, per [`references/line-endings.md`][line-endings] "Editing
discipline". After merge, check the box below and reconcile the repo's `registry/repos.json`
entry per [GOVERNANCE.md "Repository Onboarding and Conformance"][governance-onboarding] if the
conversion surfaced anything the registry didn't already record.

For an operational repo whose `lineEndings` is `crlf`: no conversion, since its global default
follows its consuming Windows-native app rather than the fleet default, per
[`references/line-endings.md`][line-endings] "Operational (config) repos", and its box below is
checked as **not applicable** rather than as converted. None currently do: `Vantage-Config` was
the one operational repo recording `crlf`, and it has since converted to the fleet `lf` default
with only its Design Center `.dc` exports pinned CRLF (ptr727/Vantage-Config#28), so its box
below is checked as converted rather than not applicable.

## Rollout Checklist

Repos and their current `registry/repos.json` `workflowModel` / `lineEndings`, from the hub's own
registry as of this doc's authorship, except where an entry records a later reclassification.

- [x] **ProjectTemplate** (`release`): hub, converted in the pull request that added this doc
- [ ] **Utilities** (`release`)
- [ ] **LanguageTags** (`release`)
- [ ] **aiopurpleair** (`release`)
- [ ] **homeassistant-purpleair** (`release`)
- [ ] **Financial-Modeling** (`release`)
- [ ] **PlexCleaner** (`release`)
- [ ] **ESPHome-NonRoot** (`release`)
- [ ] **VSCode-Server-DotNetCore** (`release`)
- [ ] **NxWitness** (`release`)
- [ ] **HomeAutomation-Config** (`release` since its 2026-09-24 reclassification from
      `operational`, `lineEndings: lf`): already on the new default's value. Verify rather than
      convert, since its own `.editorconfig`/`.gitattributes` may still carry the old redundant
      per-type LF pins the hub dropped.
- [x] **KiCadLibrary** (`release`): converted in ptr727/KiCadLibrary#57, with its vendor STEP,
      DXF, and SVG files pinned `-text` beside a matching `.editorconfig` `unset` section, and PDF
      and PNG pinned `binary`
- [ ] **EspDinIoT** (`release`)
- [ ] **ESPHome-Config** (`operational`, `lineEndings: lf`): verify, same reasoning as
      HomeAutomation-Config
- [ ] **HomeAssistant-Config** (`operational`, `lineEndings: lf`): verify, same reasoning as
      HomeAutomation-Config
- [ ] **DevKitCIoT** (`release`)
- [ ] **PhotoCleaner** (`release`)
- [ ] **MediaTools** (`release`)
- [ ] **AudioCleaner** (`release`)
- [x] **Vantage-Config** (`operational`, `lineEndings: lf` since its ptr727/Vantage-Config#28
      conversion): converted outside this rollout, with only its Design Center `.dc` exports
      pinned CRLF, not the "not applicable" case above
- [ ] **HolidayLights** (`release`)
- [ ] **Blog** (`release`, `lineEndings: lf`): already on the new default's value, a documented
      release-repo exception before this rollout, per its `registry/repos.json` `driftNotes`.
      Verify rather than convert, same reasoning as **ESPHome-Config** and
      **HomeAssistant-Config** above (not **Vantage-Config**, whose row above records an actual
      conversion rather than an already-`lf` repo to verify).

<!-- Repo -->

[fleet-map]: ./fleet-map.md
[governance-line-endings]: ../GOVERNANCE.md#line-endings
[governance-onboarding]: ../GOVERNANCE.md#repository-onboarding-and-conformance
[line-endings]: ../.agents/skills/comment-and-doc-style/references/line-endings.md
[branching-and-release-model]: ../.agents/skills/branching-and-release-model/SKILL.md

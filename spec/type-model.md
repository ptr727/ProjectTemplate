# Project Type Model

Companion to [section-model.md][section-model] and [fidelity-model.md][fidelity-model]. Those define how carried *content* is verified. This one defines how a repo's **types** (what it is built from and for) are declared, validated, and checked. It is the ground truth an agent or human consults before adding a type, a profile, or a type check, not a judgment re-derived each session.

**Rollout status.** This model is being wired in stages. Where a rule below names a check `audit.py` does not yet run, or a schema field not yet defined, this doc is the contract that implementation realizes.

## Declaration is the source of truth

A repo's types are **declared** in its [registry/repos.json][repos] entry (`types`), and a language type may also declare a **profile** (below). The audit runs the checks for each declared type plus the cross-cutting dimensions. Declaration, not inference, is authoritative: the registry states what the repo *is*, and [project-types.json][types] holds each type's requirements and checks.

This mirrors the fleet principle that *the registry is ground truth about reality, not intent* (the `setup.driftnotes.current` check, over the registry `driftNotes` field): a declaration is a claim about the repo that must match what the repo actually contains.

## Detection validates, it does not classify

Each type in `project-types.json` carries `detect` patterns (files or markers that evidence the type). Detection is a **validator and a discovery aid**, never an auto-classifier. It checks declarations against reality and surfaces candidates, but it does not decide a repo's types on its own. The two axes give four cases:

| | detected | not detected |
| --- | --- | --- |
| **declared** | consistent, so the checks run | **false declaration**, a finding (e.g. `cpp` declared, no C/C++ files present) |
| **not declared** | **discovery advisory**, so declare it or mark it ignored | nothing to do |

The undeclared-but-detected advisory has three honest resolutions, all explicit intent, never silent:

- **declare** it, so its checks run.
- **ignore** it with an explicit suppression carrying a reason (the driftNote shape), for a language deliberately not tracked (vendored third-party code, an incidental snippet).
- leave it as a standing advisory until decided.

A false declaration is always a finding: a claim the repo does not back is drift, the same way a stale driftNote is.

## Profiles: build vs lint-only

A language type is present at one of two **depths**, declared as its `profile`:

- **build** - the language is compiled, tested, and/or packaged in this repo. Its full check set applies (style, type-check, tests, coverage, packaging).
- **lint-only** - the language is present and style-checked here, but not built: there is no build/test/package for it in this repo. Only its lint/style/type-check checks apply. Build, test, coverage, and packaging checks are N/A.

Each check may declare the **minimum profile** it needs via a `minProfile` field. A check without one applies at every profile, and a check with `minProfile: build` applies only at `build`. So lint/style/type-check checks omit it, while build/test/coverage/package checks set `build`. The audit uses the declared profile to hold the coverage requirement (the CODECOV_TOKEN secret and the codecov.yml file) N/A for a lint-only language, replacing the older per-check "N/A for the SCRIPTS profile" prose.

The profile is **declared and validated**, not merely detected. `python` already reads its shape structurally from `pyproject.toml` (a uv PROJECT with tests and a lockfile, versus stdlib SCRIPTS tooling). That structural read becomes the profile **validator**. A declared `python` profile that contradicts the pyproject shape is a false declaration. One concept (the declared profile), checked by detection, rather than two ways to classify.

### Consequence for cross-cutting checks

A cross-cutting check that presumes a built, tested language must respect the profile. In particular the coverage checks (the `CODECOV_TOKEN` secret and the `codecov.yml` file presence) are **profile-aware**: they are N/A for a language whose declared profile has no tests. A lint-only language must never manufacture a coverage finding.

## Languages

Language types carry the style and type-check requirements for their language, gated by profile. A language that is only ever linted in the fleet is `lint-only` by nature and defines no build/test/package checks:

- **cpp** - C/C++ present for style only. The check of record is **clang-format** (a shared config driving the editor, the CLI, and CI, a `parity.lang` arm), feeding the operational lint CI. Deeper semantic and static analysis is intentionally out of scope here. For a codegen or config repo the C++ is scaffolded and completed by its downstream toolchain (an ESPHome compile), which does the compilation-time checking, and clang-tidy would need a compile database the repo does not have. A repo's `.h` is read as C++ by context (Arduino/ESPHome), since the extension alone is ambiguous.

## Generators

A **generator** type is what a repo builds its deliverable *with*, where the deliverable is not code: a static-site generator, a documentation builder. It is named for the generator (`hugo`) rather than for the transport that ships the result, because what a repo builds and where the result lands are separate axes. The destination lives in the registry `publish[]` entry (`{ target, mechanism }`), so a repo changes transport without changing type, and a second transport is a new **mechanism** rather than a new type. Baking the transport into the type is what makes the set explode combinatorially: one generator over two transports would otherwise need two types.

There is no `static-site` to `hugo` hierarchy while the type has one member. Instead each check's `assert` is phrased without naming the generator wherever the requirement generalizes (the URL-contract gate and its length floor, the rendered output never committed, the generator pinned by version and digest, vendored-dependency provenance), and names it only where a generator-specific construct *is* the letter, such as a build flag. When a second generator joins the fleet, promoting the generic checks to a shared type is then a registry edit rather than a rewrite, which is the property the phrasing rule exists to preserve. Paying for that abstraction at one member would be the more expensive mistake.

A generator type declares no `profiles`. Build versus lint-only is a depth of *language* presence, so a profile on a generator type would assert nothing, and `spec/validate.py` rejects a declared profile whose type does not define one.

## Changing the type set carries review weight

The set of types, their profiles, and each type's checks is governed, like the section and fidelity models.

- **Adding a type or a check** declares a new requirement for every repo that carries it. Add it to `project-types.json`, to the schema where the shape changes, and to this doc in the same change.
- **Declaring or changing a repo's type or profile** is a claim about the repo. Detection validates it, and a contradiction is a finding to reconcile, not a liberty.
- **A detected-but-undeclared language** is drift to resolve (declare or ignore-with-reason), not silently accepted.

## Enforcement

`registry/repos.json` declares each repo's types and profiles. [validate.py][validate] proves the declarations are well-formed against [project-types.schema.json][schema]. [audit.py][audit] runs each declared type's checks at its profile, validates declarations against detection (false declaration, discovery advisory, honored ignores), and holds profile-gated checks N/A off-profile.

<!-- Internal -->

[audit]: ./audit.py
[fidelity-model]: ./fidelity-model.md
[repos]: ../registry/repos.json
[schema]: ./project-types.schema.json
[section-model]: ./section-model.md
[types]: ./project-types.json
[validate]: ./validate.py

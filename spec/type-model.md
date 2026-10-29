# Project Type Model

Companion to [section-model.md][section-model] and [fidelity-model.md][fidelity-model]. Those define how carried *content* is verified. This one defines how a repo's **types** (what it is built from and for) are declared, validated, and checked. It is the ground truth an agent or human consults before adding a type, a profile, or a type check. It is not a judgment re-derived each session.

**Rollout status.** This model is being wired in stages. A rule below may name a check `audit.py` does not yet run, or a schema field not yet defined. In that case this doc is the contract that implementation realizes.

## Declaration is the source of truth

A repo's types are **declared** in its [registry/repos.json][repos] entry (`types`), and a language type may also declare a **profile** (below). The audit runs the checks for each declared type plus the cross-cutting dimensions. Declaration, not inference, is authoritative: the registry states what the repo *is*, and [project-types.json][types] holds each type's requirements and checks.

This mirrors the fleet principle that *the registry is ground truth about reality, not intent* (the `setup.driftnotes.current` check, over the registry `driftNotes` field). A declaration is a claim about the repo that must match what the repo actually contains.

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
- **lint-only** - the language is present and style-checked here, but not built or packaged in this repo. Its lint/style/type-check checks apply. Build and packaging checks are N/A. Test and coverage checks are N/A too, except for Python. Python's lint-only directories still owe a suite run under coverage.

Python's profile belongs to each declared Python directory rather than to the repository, per [CODESTYLE.md][codestyle] "Python", so one repository may hold both depths. The registry therefore declares no `python` profile. The registry's `pythonDirectories` lists the directories, and each one's `pyproject.toml` shape states its profile.

Each check may declare the **minimum profile** it needs via a `minProfile` field. A check without one applies at every profile. A check with `minProfile: build` applies only at `build`. So lint/style/type-check checks omit it, while build and package checks set `build`. A coverage check for a language other than Python sets it too. The coverage requirement (the CODECOV_TOKEN secret and the codecov.yml file, or its .codecov.yml alternative) is N/A for a lint-only language other than Python. Otherwise it follows tests: the audit reads the repo's tree for tests and holds the requirement N/A where it finds none.

A registry profile is **declared and validated**, not merely detected. Python is the exception, because its structural read of each directory's `pyproject.toml` already is the profile. A repository-wide value could not describe a repository holding both depths. It would also be a second classification of a shape that already states one. So [validate.py][validate] refuses a `python` entry in `profiles`.

### Consequence for cross-cutting checks

A cross-cutting check that presumes a built, tested language must respect the profile. In particular the coverage checks (the `CODECOV_TOKEN` secret and the `codecov.yml` or `.codecov.yml` file presence) are **tests-aware**. They are N/A for a lint-only language other than Python, and for any language the repo carries no tests for. A language with no tests here must never manufacture a coverage finding. That holds whether it is lint-only or a package-only build language whose tests live elsewhere or are not yet written.

## Languages

Language types carry the style and type-check requirements for their language, gated by profile. A language that is only ever linted in the fleet is `lint-only` by nature and defines no build/test/package checks:

- **cpp** - C/C++ present for style only. The check of record is **clang-format** (a shared config driving the editor, the CLI, and CI, a `parity.lang` arm), feeding the operational lint CI. Deeper semantic and static analysis is intentionally out of scope here. For a codegen or config repo, the C++ is scaffolded and completed by its downstream toolchain (an ESPHome compile), which does the compilation-time checking. For such a repo, clang-tidy would need a compile database the repo does not have. A repo's `.h` is read as C++ by context (Arduino/ESPHome), since the extension alone is ambiguous.

## Generators

A **generator** type is what a repo builds its deliverable *with*, where the deliverable is not code: a static-site generator, a documentation builder. It is named for the generator (`hugo`) rather than for the transport that ships the result. That is because what a repo builds and where the result lands are separate axes. The destination lives in the registry `publish[]` entry (`{ target, mechanism }`), so a repo changes transport without changing type. A second transport is thus a new **mechanism** rather than a new type. Baking the transport into the type is what makes the set explode combinatorially: one generator over two transports would otherwise need two types.

There is no `static-site` to `hugo` hierarchy while the type has one member. Instead each check's `assert` is phrased without naming the generator wherever the requirement generalizes. Examples are the URL-contract gate and its length floor, the rendered output never committed, the generator pinned by version and digest, and vendored-dependency provenance. A check names the generator only where a generator-specific construct *is* the letter, such as a build flag. When a second generator joins the fleet, promoting the generic checks to a shared type is then a registry edit rather than a rewrite. That is the property the phrasing rule exists to preserve. Paying for that abstraction at one member would be the more expensive mistake.

A generator type declares no `profiles`. Build versus lint-only is a depth of *language* presence, so a profile on a generator type would assert nothing. `spec/validate.py` rejects a declared profile whose type does not define one.

## Runtime Libraries

A **runtime library** type declares that the repo's own runtime executes a library the hub hosts. The type's token selects the library's tree in [`spec/files.json`][files], and the repo carries it byte-for-byte. `host-secrets` is the one such type, for a repo whose shell scripts read a credential from disk. It carries `lib/sh` from the hub to `lib/fleet/sh`. Its `detect` entry surfaces such a script as a discovery advisory, and the declaration is what makes the type's check apply, as for any type. A runtime library type declares no `profiles`, for the reason a generator type declares none.

## Changing the type set carries review weight

The set of types, their profiles, and each type's checks is governed, like the section and fidelity models.

- **Adding a type or a check** declares a new requirement for every repo that carries it. Add it to `project-types.json`, to the schema where the shape changes, and to this doc in the same change.
- **Declaring or changing a repo's type or profile** is a claim about the repo. Detection validates it, and a contradiction is a finding to reconcile, not a liberty.
- **A detected-but-undeclared language** is drift to resolve (declare or ignore-with-reason), not silently accepted.

## Enforcement

`registry/repos.json` declares each repo's types and profiles. [validate.py][validate] proves the declarations are well-formed against [project-types.schema.json][schema]. [audit.py][audit] runs each declared type's checks at its profile, validates declarations against detection (false declaration, discovery advisory, honored ignores), and holds profile-gated checks N/A off-profile.

<!-- Internal -->

[audit]: ./audit.py
[codestyle]: ../CODESTYLE.md#python
[fidelity-model]: ./fidelity-model.md
[files]: ./files.json
[repos]: ../registry/repos.json
[schema]: ./project-types.schema.json
[section-model]: ./section-model.md
[types]: ./project-types.json
[validate]: ./validate.py

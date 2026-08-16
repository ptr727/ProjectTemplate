# Audit: `<repo>`

- **Audited branch:** main (`<sha>`)
- **Types:** `<from registry, or resolved>`
- **Verdict:** operational | not operational
- **Date:** `<YYYY-MM-DD>`

## Develop Drift

`develop` vs `main`: ahead `<n>`, behind `<n>`. Classify by **content**, not commit count: promotion merge commits leave `main` permanently ahead with identical trees (benign, inherent to merge-commit promotions - a cherry-pick would be an empty no-op). A drift finding only when `main` carries content `develop` lacks; the audit's content-based branch check is the authority.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| csharp | | | | |
| nuget | | | | |
| pypi | | | | |
| python | | | | |
| console | | | | |
| docker | | | | |
| branch-model | | | | |
| repo-setup | | | | |
| linter-parity | | | | |
| recurring-violations | | | | |
| readme-structure | | | | |
| workflow (WORKFLOW.md 5A/5B) | | | | |

Verdict values: pass | drift | defect | N/A. Remove rows that are N/A for the repo's types, or mark them N/A.

## Defects (most severe first)

1. `<defect>` - input/condition -> observed vs expected, at `file:line`.

## Drift Findings

- `<letter miss, intent satisfied>` - `file:line`.

## Proposed Registry / Spec Updates

- `<e.g. resolve classificationPending: types = [...]>`

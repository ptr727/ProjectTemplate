# Audit: <repo>

- **Audited branch:** main (`<sha>`)
- **Types:** <from registry, or resolved>
- **Verdict:** operational | not operational
- **Date:** <YYYY-MM-DD>

## Develop Drift

`develop` vs `main`: ahead <n>, behind <n>. <in sync | stale | diverged - a drift finding if not in sync>

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

1. <defect> - input/condition -> observed vs expected; `file:line`.

## Drift Findings

- <letter miss, intent satisfied> - `file:line`.

## Proposed Registry / Spec Updates

- <e.g. resolve classificationPending: types = [...]>

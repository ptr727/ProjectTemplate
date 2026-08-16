# Agent Token Cost: Measurement and Response (Hub-Only)

Why the [`AGENTS.md`][agents] "Context and Delegation Discipline" rules exist, what they were measured against, and what remains open. This doc is **hub-only** and records a measurement, so it is not carried downstream. Without it those rules read as unmotivated preference and a later agent deletes them.

Measured over 220 Claude Code transcripts and 383 pull requests, June to July 2026. Every figure comes from `message.usage` records de-duplicated by request id, or from the GitHub GraphQL API. Costs use the first-party API rate: Opus $5 in and $25 out per million tokens, cache read at 0.1x, cache write at 1.25x on the 5-minute TTL and 2x on the 1-hour.

## The Equation

```text
cost ~= 0.1 x (requests) x (average context)
```

A session's context grows monotonically and every request re-reads all of it at the cache-read rate, so a token added early is paid for again on every request that follows. Both factors were large: 18,254 requests at an average of about 386,000 tokens of context, for **7.05 billion prompt tokens** and roughly **$5,086**.

| Component | Cost | Share |
| --- | ---: | ---: |
| Cache reads | $3,466 | 68.1% |
| Cache writes | $1,083 | 21.3% |
| Output | $528 | 10.4% |
| Fresh input | $9 | 0.2% |

**Output verbosity is not the lever.** Output was 0.30% of prompt volume and 10.4% of cost, so instructing an agent to write less prose targets a tenth of the bill at best. The cache is also working correctly at a 98.4% read share. The problem is the volume being re-read, not the hit rate.

## The Root Cause: Sessions Outliving Their Task

| Percentile | Context per request |
| --- | ---: |
| p50 | 418,063 |
| p75 | 671,139 |
| p90 | 861,035 |
| p99 | 987,530 |

Sessions ran repeatedly into the 1M context ceiling, and **55% of all spend sat on requests carrying more than 600k**. The cause was not one long task. The largest session ran **88 branches over 11 days in one context**, 4,151 requests and 2.12 billion prompt tokens, with 165 branch switches. Context at its first branch switch was 71,045 tokens, so by the 88th task every request was carrying the residue of the previous 87. That single session cost about $1,060 in cache reads.

A contiguous single-branch piece of work is small by comparison: median 16 requests, p90 65, mean 36. The waste was never within a deliverable, it was across unrelated ones.

### Counterfactual

Resetting context at each branch switch, holding request count and within-task growth constant, takes 6.93 billion prompt tokens to 1.24 billion. Sensitivity to what a fresh session must re-read:

| Re-read penalty per task | Cache-read cost | Saving |
| ---: | ---: | ---: |
| 0 | $622 | 82% |
| 25,000 tokens | $815 | 76% |
| 50,000 | $1,007 | 71% |
| 100,000 | $1,393 | 60% |
| 150,000 | $1,778 | 49% |

Even at 150,000 tokens, over four times the observed 34,000-token session baseline, the saving is 49%. This is why "one deliverable, one session" is a rule rather than a suggestion.

## Attribution

Two measured attributions. Both **overlap** with the counterfactual above rather than adding to it, because both are driven by how many requests remain after a token enters context.

### Re-reading the governance set: $706, 20% of the cache-read bill

| File | Reads | Cost |
| --- | ---: | ---: |
| `AGENTS.md` (pre-split, 87 KB) | 216 | $273 |
| `WORKFLOW.md` | 245 | $253 |
| `README.md` | 125 | $96 |
| `.github/copilot-instructions.md` | 48 | $36 |
| `CODESTYLE.md` | 56 | $32 |
| `AUDIT.md` | 27 | $17 |

Median 199 requests remained in the session after such a read, p90 6,005, and only 36.9% of all reads used a range. `AGENTS.md` was **not** auto-loaded by any harness (the session baseline was 33,000 to 37,000 tokens with none of its text present), so the whole cost came from explicit whole-file reads.

The rule text could not simply be cut: 16 of 18 sections were declared `fidelity: verbatim` and carried byte-identically to the fleet. So the file was split instead (`AGENTS.md` from 87,457 to 7,789 bytes as a router, with the rule text in [`GOVERNANCE.md`][governance]), and reading one section now costs about 3,200 bytes against 87,457.

### GitHub orchestration: $938, 27% of the cache-read bill

| Category | Requests | Cost | Avg result |
| --- | ---: | ---: | ---: |
| Copilot review polling | 2,848 | $634 | 574 B |
| Other `gh` | 590 | $106 | 1,558 B |
| Merge | 422 | $82 | 576 B |
| CI status polling | 463 | $80 | 545 B |
| Create pull request | 190 | $36 | 384 B |

This inverts the obvious fix. The `gh` output was already tiny at 574 bytes average, while the turns carrying it were billed against about 417,000 tokens of context, a ratio near 960 to 1. **100% of gh-issuing turns contained exactly one gh call**, so there was no batching at all. Compressing gh output saves nothing and removing turns saves everything, which is what [`scripts/pr_review.py`][pr-review] does.

## Review Round-Trips

383 pull requests, 1,047 Copilot findings, 783 review rounds: mean 2.42 rounds per reviewed pull request, median 2, **maximum 15**. 54% needed two or more. One case, a single-file change, took 14 rounds.

1,003 of the 1,047 findings were resolved, so the reviewer was finding real defects. This is rework to pre-empt, not noise to suppress. Findings by file type were 39% Markdown, 21% Python, 19% workflow YAML. An audit put roughly half in categories a deterministic check can catch before a push, which is what [`scripts/prose_lint.py`][prose-lint] and [`scripts/repo_gate.py`][repo-gate] cover.

## What Is Not Settled

- **Model and effort tiering is unquantified.** The mix was 17,851 Opus requests, 390 Haiku, and zero Sonnet, with a global high effort setting, so the headroom is obvious. The saving is not: establishing it needs deliberate paired runs, and no number is claimed here.
- **Fresh-context self-review is unproven.** An estimate put 44% of findings within reach of a reviewer reading the diff against the docs, but that was an estimate, and a same-model no-context review was tried and found far less than Copilot did. Removing context does not remove shared priors. Treat it as an experiment, and give any retest the finding taxonomy rather than a generic instruction to be adversarial.
- **The semicolon and dash rules are warn-first, and the charset and duplicate-word rules gate, being clean tree-wide.** Compare their hits against the next batch of review findings before enforcing them, and do not claim the mechanical share until that comparison exists.

## Re-measuring

The measurement is reproducible from the transcripts in about a minute, so the effect of these changes is checkable without instrumentation. Re-run the aggregation over `message.usage` records and compare the average context per request and the branch count per session against the figures above.

<!-- Internal -->

[agents]: ../AGENTS.md
[governance]: ../GOVERNANCE.md
[pr-review]: ../scripts/pr_review.py
[prose-lint]: ../scripts/prose_lint.py
[repo-gate]: ../scripts/repo_gate.py

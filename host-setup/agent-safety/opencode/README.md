# opencode Write-Safety Gap

No hook implements [`../README.md`][spec]'s requirements for opencode yet. Tracked at
[issue #781][issue-781].

## What To Keep Enabled Meanwhile

Keep opencode's own permission model enabled. The carried repository rules (`GOVERNANCE.md`
"Repository Boundaries and Write Safety" and the rest of the fleet's prose) remain the only
behavioral layer in a fleet checkout until a hook exists -- there is no mechanical backstop for
opencode today, which means a mistake that the Claude Code hook would deny goes through unblocked
in an opencode session.

## Implementing Against the Spec

[`../README.md`][spec] states each requirement as an agent-agnostic decision rule, not tied to any
one hook API. [`../claude/gh-write-guard.py`][claude-hook] is a worked reference implementation of
the spec's currently-implemented requirements against Claude Code's `PreToolUse` hook, including
its tokenizer, its self-test matrix (`--selftest`), and its documented fail-open/fail-closed
choices per requirement -- useful as a model for argv parsing and edge cases, not as something to
port line for line, since opencode's own permission-model extension points differ from Claude
Code's hook shape. Whatever mechanism opencode offers for intercepting or gating a command before
it runs is the place to implement the spec's requirements. If opencode offers no such extension
point at all, that finding belongs on [issue #781][issue-781], not silently worked around.

## Auditing

Once an opencode-side implementation exists, audit it the way [`../README.md`][spec] "Auditing an
Implementation Against This Spec" describes: run its own self-test and check every case against
the spec's requirements list, not against `gh-write-guard.py`'s source.

<!-- Repo -->
[spec]: ../README.md
[claude-hook]: ../claude/gh-write-guard.py
[issue-781]: https://github.com/ptr727/ProjectTemplate/issues/781

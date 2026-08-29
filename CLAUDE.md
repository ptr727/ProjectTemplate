# Claude Code Entry Point

@AGENTS.md

Claude Code reads `CLAUDE.md`, not `AGENTS.md`, so a repo carrying only `AGENTS.md` never
reaches a Claude Code session on its own, the import line above is what makes it load. Every
rule lives in `AGENTS.md` and `GOVERNANCE.md`, which are provider-agnostic by design so Codex
and opencode read the same rules with no separate copy. Do not add content here beyond the
import line, a Claude-specific addition would be exactly the per-provider duplication those two
files exist to avoid.

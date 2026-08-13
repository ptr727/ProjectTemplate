# Fleet Skills

Canonical source for the fleet's Claude Code / opencode / Codex Skills, one directory per skill: `<name>/SKILL.md` plus optional `scripts/` and `references/`. This is the only place a skill's content is hand-authored. Everything else derived from it is generated, never hand-edited.

Codex and opencode read this directory directly (`.agents/skills/<name>/SKILL.md`), no install step required, walking from a downstream repo's working directory up to its own repository root. Claude Code does not scan this path. `scripts/build_dist.py` generates a Claude-plugin-compatible copy at `.claude-plugin/fleet-skills/`, published through `.claude-plugin/marketplace.json`.

Empty today: this is Phase 0 scaffolding. See `AGENTS.md` for how a repo depends on these skills and `scripts/README.md` for `build_dist.py` and the installer.

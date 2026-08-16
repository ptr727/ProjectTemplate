# ProjectTemplate

Agent enablement for a fleet of repositories: autonomy and repeatable quality inside guardrails.

## Release History

- Version 2.0:
  - Repurposed from a .NET sample-project template into a governance and workflow-audit catalog: the shared fleet rules (`AGENTS.md`, `CODESTYLE.md`, `WORKFLOW.md`), a machine-readable `spec/`, the fleet `registry/`, per-repo audit `reports/`, branch rulesets, and three procedures an agent routes between by what a repository actually holds: `STANDUP.md` from nothing to operational, `AUDIT.md` to measure without changing anything, and `RESYNC.md` to bring a repository that has fallen behind back into line. Adds a host tool contract with version floors (`spec/host-tools.json` and a carried `host-tools.json` per repository), and a derived detector for files the hub hosts rather than carries, so a retired file is found rather than remembered. Ships no application code, and the old sample project and its build pipeline are removed.
- Version 1.0:
  - .NET sample-project template with a build and publish pipeline.

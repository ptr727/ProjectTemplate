# pr-agent-settings

Qodo's organization-wide configuration for the `ptr727` repositories.

## Purpose

[Qodo][qodo-link] reads a repository named `pr-agent-settings` under an organization or account as that owner's central configuration. It reads `.pr_agent.toml` from this repository's default branch for every repository under the owner. A repository carrying its own `.pr_agent.toml` on its default branch overrides each key that file sets, and inherits the rest from here.

A fleet repository chooses which reviewers run, on which branches, and when, in its own `.github/agent-policy.json`. That file is rendered from the [ProjectTemplate][projecttemplate-link] registry, together with the repository's `.coderabbit.yaml` and `.pr_agent.toml`. Every rendered file sets `config.disable_auto_feedback`. So once present, that key here reaches only a repository the registry renders no files for, such as a new one. It turns automatic feedback off, so it starts no review nobody asked for.

## Status

Qodo reads a repository's own file only from its default branch. So a central default with automatic feedback off silences Qodo wherever a repository's own file has not reached that branch. `.pr_agent.toml` is therefore present here only while every fleet repository carries its own `.pr_agent.toml` on its default branch, setting `config.disable_auto_feedback`. [`scripts/central_config.py`][central-config-link] `write` adds the file when that holds and removes it when it does not.

## Where the Rules Live

This repository holds configuration only. The rules it serves live in ProjectTemplate:

- [Pull Request Reviewer Reference][reviewer-reference-link] describes each reviewer bot and how its configuration is committed. Its "Review Configuration" section covers the review policy.
- [`spec/agent-policy.schema.json`][agent-policy-schema-link] defines the policy file, and [`spec/agent-policy.default.json`][agent-policy-default-link] is the default a repository inherits where its own file states none.
- [`GOVERNANCE.md`][governance-link] "PR Review Etiquette" governs how a review is requested, triaged, and resolved.
- [Repository Configuration][repo-config-link] describes `repo-config/configure.sh`, which applies each repository's bot settings.

## Changing the Configuration

ProjectTemplate writes this repository with [`scripts/central_config.py`][central-config-link]. Make a change there and carry it here by a pull request. Never edit this repository in place. The [configuration reference][qodo-config-link] names each option.

<!-- External links -->
[projecttemplate-link]: https://github.com/ptr727/ProjectTemplate
[qodo-config-link]: https://qodo-merge-docs.qodo.ai/usage-guide/configuration_options/
[qodo-link]: https://www.qodo.ai

<!-- ProjectTemplate links -->
[agent-policy-default-link]: https://github.com/ptr727/ProjectTemplate/blob/main/spec/agent-policy.default.json
[agent-policy-schema-link]: https://github.com/ptr727/ProjectTemplate/blob/main/spec/agent-policy.schema.json
[central-config-link]: https://github.com/ptr727/ProjectTemplate/blob/main/scripts/central_config.py
[governance-link]: https://github.com/ptr727/ProjectTemplate/blob/main/GOVERNANCE.md#pr-review-etiquette
[repo-config-link]: https://github.com/ptr727/ProjectTemplate/blob/main/docs/repo-config.md
[reviewer-reference-link]: https://github.com/ptr727/ProjectTemplate/blob/main/docs/pr-reviewer-reference.md

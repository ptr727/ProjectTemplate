# coderabbit

CodeRabbit's organization-wide configuration for the `ptr727` repositories.

## Purpose

[CodeRabbit][coderabbit-link] reads a repository named `coderabbit` under an organization or account as that owner's central configuration. A repository that carries no `.coderabbit.yaml` of its own uses the `.coderabbit.yaml` here. A repository that carries one uses its own file instead.

A fleet repository chooses which reviewers run, on which branches, and when, in its own `.github/agent-policy.json`. That file is rendered from the [ProjectTemplate][projecttemplate-link] registry, together with the repository's `.coderabbit.yaml` and `.pr_agent.toml`. So once present, the default here reaches only a repository the registry renders no files for, such as a new one. It turns automatic review off, so it starts no review nobody asked for.

## Status

A central default with automatic review off silences CodeRabbit on every repository that lacks its own file. So `.coderabbit.yaml` is present here only while every fleet repository carries its own `.coderabbit.yaml` on its default branch, setting `reviews.auto_review.enabled`. [`scripts/central_config.py`][central-config-link] `write` adds the file when that holds and removes it when it does not.

## Where the Rules Live

This repository holds configuration only. The rules it serves live in ProjectTemplate:

- [Pull Request Reviewer Reference][reviewer-reference-link] describes each reviewer bot and how its configuration is committed. Its "Review Configuration" section covers the review policy.
- [`spec/agent-policy.schema.json`][agent-policy-schema-link] defines the policy file, and [`spec/agent-policy.default.json`][agent-policy-default-link] is the default a repository inherits where its own file states none.
- [`GOVERNANCE.md`][governance-link] "PR Review Etiquette" governs how a review is requested, triaged, and resolved.
- [Repository Configuration][repo-config-link] describes `repo-config/configure.sh`, which applies each repository's bot settings.

## Changing the Configuration

ProjectTemplate writes this repository with [`scripts/central_config.py`][central-config-link]. Make a change there and carry it here by a pull request. Never edit this repository in place. The [settings reference][coderabbit-config-link] names each option.

<!-- External links -->
[coderabbit-config-link]: https://docs.coderabbit.ai/reference/configuration
[coderabbit-link]: https://www.coderabbit.ai
[projecttemplate-link]: https://github.com/ptr727/ProjectTemplate

<!-- ProjectTemplate links -->
[agent-policy-default-link]: https://github.com/ptr727/ProjectTemplate/blob/main/spec/agent-policy.default.json
[agent-policy-schema-link]: https://github.com/ptr727/ProjectTemplate/blob/main/spec/agent-policy.schema.json
[central-config-link]: https://github.com/ptr727/ProjectTemplate/blob/main/scripts/central_config.py
[governance-link]: https://github.com/ptr727/ProjectTemplate/blob/main/GOVERNANCE.md#pr-review-etiquette
[repo-config-link]: https://github.com/ptr727/ProjectTemplate/blob/main/docs/repo-config.md
[reviewer-reference-link]: https://github.com/ptr727/ProjectTemplate/blob/main/docs/pr-reviewer-reference.md

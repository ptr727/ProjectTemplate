# README Structure

The preferred `README.md` shape for a fleet project. The audit's `readme-structure` dimension checks a repo's README against this: the required sections in this order (to the letter where a section applies, to intent where a project legitimately has nothing to say). Sections that do not apply to a project type are N/A, not a defect (a library has no "Installation" of a running app; a source-only repo has no "Usage").

## Required Sections and Order

1. **Title (`# <Name>`)** - the repo name, followed by a one-line description of what it is.
2. **Shields** - build/release status and version badges immediately under the title, one logical line per group with a trailing backslash for the hard break. Alphabetize the shield link-reference definitions block at the bottom of the file (per AGENTS.md Markdown rules).
3. **Build and Distribution** - where releases and packages are published (GitHub Releases, NuGet, PyPI, Docker Hub), and a short **Release Notes** summary (full history in `HISTORY.md`).
4. **Getting Started** - the shortest path to using the project.
5. **Use Cases** - what problems it solves (optional for a library whose purpose is obvious from the description).
6. **Installation** - how to install or pull it, per distribution channel. N/A for source-only.
7. **Configuration** - settings, environment, config files. N/A when there is nothing to configure.
8. **Usage** - commands, API, or integration steps. For a CLI, a command quick-reference and the global options. N/A for source-only.
9. **Questions or Issues** - where to file issues and ask questions.
10. **Development Environment** - how to build, test, and lint locally; commit-signing setup; devcontainer notes. Point at shared docs rather than restating them.
11. **3rd Party Tools** - notable dependencies and their licenses, where relevant.
12. **License** - a pointer to `LICENSE`.

## Style

- Write in the current state, not as a change from a prior one (AGENTS.md Documentation Style).
- One logical paragraph per line; no hard-wrap.
- Title-case headings with lowercase short bind words.
- US English, ASCII only (no em-dash; use a spaced hyphen), straight quotes.
- Verify every quantitative claim (counts, versions, supported platforms) against current code.
- A project README describes only that project - no cross-repo references and no template or inheritance framing.

## Docker Hub README

A repo that publishes a Docker image keeps a **separate** `Docker/README.md` for the Docker Hub repository overview: Docker Hub's description has a much smaller size limit than a project README, so it carries a trimmed overview, not the full README. It is published by the docker-readme workflow task, not copied from the root README.

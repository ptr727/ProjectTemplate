"""The review policy's reads off a pull request payload, for the readers that may not load the policy.

This is a library module and has no command line. `gql` attaches the parsed policy to each payload
it reads, under `POLICY_KEY`. A reader may not import `pr_review_policy`, which reads files and runs
`gh`. So the key and the two reads every consumer of it repeats live here instead. The writer
and the readers name this one constant, so they cannot disagree on it.
"""

from __future__ import annotations

from typing import Any

POLICY_KEY = "_reviewPolicy"


def default_branch(pr: dict) -> str:
    """The base repository's default branch off `pr`, empty where it was not read."""
    return ((pr.get("baseRepository") or {}).get("defaultBranchRef") or {}).get("name") or ""


def bot_entry(pr: dict | None, name: str) -> Any:
    """The policy's settings for bot `name` off `pr`, None where no policy rides the payload."""
    policy = (pr or {}).get(POLICY_KEY)
    return policy.bot(name) if policy is not None else None

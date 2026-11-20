"""An interface unit's `requireTokens` contract key, read against the whole caller file.

`requireTokensInJob` reads one job, so a token a stub owes outside every job needs a key of its own.
The merge-bot stub is the case it exists for. Its `on:` types must name `auto_merge_disabled`, or the
hub task's re-arm job never runs there and a disabled auto-merge stalls the pull request.
"""

from collections.abc import Mapping
from typing import Any

Finding = tuple[str, str]


def require_tokens_findings(path: str, contract: Mapping[str, Any], view: str) -> list[Finding]:
    """DRIFT for each `requireTokens` entry absent from `view`, the whole file's code view."""
    return [
        ("DRIFT", f"interface: {path} missing required '{token}'")
        for token in contract.get("requireTokens", [])
        if token not in view
    ]

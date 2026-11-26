"""An interface unit's contract tokens, each read against the file's code view or one job's.

`artifactNameToken` and `requireTokens` read the whole file, so a token a stub owes outside every
job is checked too. A job name can satisfy such a token. `requireTokens` reads the view with a
leading newline, so a token anchored on one matches the first key too. In that view a key holding
nothing, an empty mapping or a bare block scalar key, reads `key: {}`. So a newline then
`permissions: {}` requires an empty workflow-level block, which neither a job-level block nor a
granted scope matches. `requireEventTypes` reads each event's `types:` list alone. A job name or a
branch filter then stands in for no subscription. `requireTokensInJob` and `forbidTokensInJob`
read one job each. The merge-bot stub's `on:` types must name `auto_merge_disabled`, or the hub
task's re-arm job never runs there and a disabled auto-merge stalls the pull request.
"""

from collections.abc import Mapping, Sequence
from typing import Any


def _shown(token: str) -> str:
    """A token as a finding prints it, a newline written `\\n` so the finding stays one line."""
    return token.replace("\n", "\\n")


def _required_view(file_view: str) -> str:
    """The file view `requireTokens` reads, an empty-valued key marked `key: {}`.

    A code view writes an empty mapping as a bare `key:` line, the same as a key with children
    under it. Here a bare key whose next line is no deeper reads `key: {}` instead.
    """
    lines = file_view.split("\n")
    depths = [len(line) - len(line.lstrip(" ")) for line in lines] + [-1]
    marked = [
        f"{line} {{}}"
        if line.endswith(":") and ": " not in line.strip() and depths[i + 1] <= depths[i]
        else line
        for i, line in enumerate(lines)
    ]
    return "\n" + "\n".join(marked)


def token_findings(
    path: str,
    contract: Mapping[str, Any],
    file_view: str,
    job_views: Mapping[str, str],
    event_types: Sequence[str] = (),
) -> list[tuple[str, str]]:
    """DRIFT for each contract token the code lacks or forbids.

    `file_view` is the whole file's code view, `event_types` the values of each event's
    `types:` list, and `job_views` each job's own, keyed by job. A job absent from `job_views` is already a requiredJobKeys finding, so its tokens are not reported.
    """
    findings: list[tuple[str, str]] = []
    tok = contract.get("artifactNameToken")
    if tok and tok not in file_view:
        findings.append(
            ("DRIFT", f"interface: {path} missing the '{tok}<branch>-<target>' artifact handoff")
        )
    required_view = _required_view(file_view)
    findings += [
        ("DRIFT", f"interface: {path} missing required '{_shown(t)}'")
        for t in contract.get("requireTokens", [])
        if t not in required_view
    ]
    findings += [
        ("DRIFT", f"interface: {path} 'on:' missing required event type '{t}'")
        for t in contract.get("requireEventTypes", [])
        if t not in event_types
    ]
    for job, toks in contract.get("requireTokensInJob", {}).items():
        if job in job_views:
            findings += [
                ("DRIFT", f"interface: {path} job '{job}' missing required '{_shown(t)}'")
                for t in toks
                if t not in job_views[job]
            ]
    for job, toks in contract.get("forbidTokensInJob", {}).items():
        if job in job_views:
            findings += [
                ("DRIFT", f"interface: {path} job '{job}' uses forbidden '{_shown(t)}'")
                for t in toks
                if t in job_views[job]
            ]
    return findings

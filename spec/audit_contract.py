"""An interface unit's contract tokens, each read against the file's code view or one job's.

`artifactNameToken` and `requireTokens` read the whole file, so a token a stub owes outside every
job, such as an `on:` activity type, is checked too. `requireTokensInJob` and `forbidTokensInJob`
read one job each. The merge-bot stub's `on:` types must name `auto_merge_disabled`, or the hub
task's re-arm job never runs there and a disabled auto-merge stalls the pull request.
"""

from collections.abc import Mapping
from typing import Any


def token_findings(
    path: str, contract: Mapping[str, Any], file_view: str, job_views: Mapping[str, str]
) -> list[tuple[str, str]]:
    """DRIFT for each contract token the code lacks or forbids.

    `file_view` is the whole file's code view and `job_views` each job's own, keyed by job. A job
    absent from `job_views` is already a requiredJobKeys finding, so its tokens are not reported.
    """
    findings: list[tuple[str, str]] = []
    tok = contract.get("artifactNameToken")
    if tok and tok not in file_view:
        findings.append(
            ("DRIFT", f"interface: {path} missing the '{tok}<branch>-<target>' artifact handoff")
        )
    findings += [
        ("DRIFT", f"interface: {path} missing required '{t}'")
        for t in contract.get("requireTokens", [])
        if t not in file_view
    ]
    for job, toks in contract.get("requireTokensInJob", {}).items():
        if job in job_views:
            findings += [
                ("DRIFT", f"interface: {path} job '{job}' missing required '{t}'")
                for t in toks
                if t not in job_views[job]
            ]
    for job, toks in contract.get("forbidTokensInJob", {}).items():
        if job in job_views:
            findings += [
                ("DRIFT", f"interface: {path} job '{job}' uses forbidden '{t}'")
                for t in toks
                if t in job_views[job]
            ]
    return findings

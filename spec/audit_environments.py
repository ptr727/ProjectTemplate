"""The secret and variable names each deployment environment holds, against the registry's declaration.

A registry `environments[]` entry declares the names its environment's two stores hold, in `secrets` and
`variables`, and an absent list declares an empty store. Every environment GitHub has is read, not only the
declared ones, so an environment the registry never names must hold nothing. That is what catches a
credential copied into an environment that must not have it.

Only names leave `gh`. The variables endpoint returns each value beside its name, so the projection to
`.name` runs inside `gh` and no value ever reaches this process. A failed read raises rather than
answering empty, since an empty answer would read as a store holding nothing and therefore as a pass.
"""

import subprocess
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

Finding = tuple[str, str]
Reader = Callable[[str, str], list[str]]
KINDS = {"secrets": "secret", "variables": "variable"}
# GitHub caps the variables endpoint at 30 a page and the other two at 100, so --paginate walks the rest.
PAGE = {"environments": 100, "secrets": 100, "variables": 30}


def names(path: str, key: str) -> list[str]:
    """The `name` of each element of `key` across every page of one REST path, or raise when gh fails.

    The host is pinned because GH_HOST would otherwise point the read at another host.
    The repository is already explicit in `path`, so GH_REPO resolves nothing here.
    """
    r = subprocess.run(
        ["gh", "api", "--paginate", "--hostname", "github.com", path, "--jq", f".{key}[].name"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if r.returncode != 0:
        tail = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "failed"
        raise RuntimeError(f"gh api {path}: {tail}")
    return r.stdout.splitlines()


def held(slug: str, read: Reader) -> dict[str, dict[str, set[str]]]:
    """Each live environment's secret and variable names, keyed by environment name and then by store."""
    live = {}
    for env in read(f"repos/{slug}/environments?per_page={PAGE['environments']}", "environments"):
        base = f"repos/{slug}/environments/{urllib.parse.quote(env, safe='')}"
        live[env] = {key: set(read(f"{base}/{key}?per_page={PAGE[key]}", key)) for key in KINDS}
    return live


def compare(
    declared: Mapping[str, Mapping[str, set[str]]], live: Mapping[str, Mapping[str, set[str]]]
) -> list[Finding]:
    """The findings from comparing what each environment holds with what the registry declares it holds.

    A declared name the store lacks is DEFECT, and so is a name held as the other kind instead.
    A held name the declaration omits is DRIFT, whether or not the registry declares its environment.
    A declared environment GitHub lacks holds nothing, so each name it declares is missing.
    """
    findings = []
    for env in sorted(set(declared) | set(live)):
        known = env in declared
        want = declared.get(env, {key: set() for key in KINDS})
        have = live.get(env, {key: set() for key in KINDS})
        for key, other in (("secrets", "variables"), ("variables", "secrets")):
            # A name held only as the other kind is one wrong-kind finding, not a missing one plus an unlisted one.
            swapped = (want[key] - have[key]) & (have[other] - want[other])
            for name in sorted(swapped):
                findings.append(
                    (
                        "DEFECT",
                        (
                            f"secrets: {name} in environment {env} is held as a {KINDS[other]} "
                            f"but declared as a {KINDS[key]}"
                        ),
                    )
                )
            absent = "" if env in live else " (the environment does not exist)"
            for name in sorted(want[key] - have[key] - swapped):
                findings.append(
                    (
                        "DEFECT",
                        f"secrets: {KINDS[key]} {name} missing from environment {env}{absent}",
                    )
                )
            # The reverse pairing, already reported above as the declared kind's wrong-kind finding.
            reported = (want[other] - have[other]) & (have[key] - want[key])
            where = (
                " is not in the registry's declaration for it (stale?)"
                if known
                else ", which the registry does not declare, so it must hold nothing"
            )
            for name in sorted(have[key] - want[key] - reported):
                findings.append(
                    ("DRIFT", f"secrets: {KINDS[key]} {name} in environment {env}{where}")
                )
    return findings


def declared_stores(entry: Mapping[str, Any]) -> dict[str, dict[str, set[str]]]:
    """Each environment the registry entry declares, with the names it declares for each store."""
    return {
        env["name"]: {key: set(env.get(key, [])) for key in KINDS}
        for env in entry.get("environments", [])
    }


def environment_findings(
    slug: str, entry: Mapping[str, Any], read: Reader | None = None
) -> list[Finding]:
    """Every finding for one repository's environment stores, read by name only.

    `names` is looked up at call time rather than bound as a default, so a test replacing it is the one read.
    """
    return compare(declared_stores(entry), held(slug, read or names))

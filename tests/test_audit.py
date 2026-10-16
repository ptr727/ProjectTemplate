#!/usr/bin/env python3
"""Drive audit_repo against a faked gh, for what only a whole run of it shows."""

from __future__ import annotations

import base64
import pathlib
import sys
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "spec"))
import audit

SLUG = "owner/Fixture"
ENTRY = {
    "name": "Fixture",
    "url": f"https://github.com/{SLUG}",
    "status": "cataloged",
    "types": ["python"],
    "hasDevelop": False,
}
CARRIED = {"pyproject.toml", "tests/test_a.py"}
HEAD = {"commit": {"sha": "c0ffee", "commit": {"tree": {"sha": "feed"}}}}
CODECOV = {
    "path": "codecov.yml",
    "alternatives": [".codecov.yml"],
    "appliesTo": ["python"],
}


class FakeGh:
    """The REST reads audit_repo makes, answered for a repository carrying exactly `tree`.

    An unanticipated read fails the test rather than answering. A guessed answer could steer
    audit_repo past the loop under test while still reading as a pass.
    """

    def __init__(self, tree: set[str]) -> None:
        self.tree = tree
        self.paths: list[str] = []

    def __call__(self, path: str, ok404: bool = False) -> Any:
        self.paths.append(path)
        base = path.split("?", 1)[0]
        if base == f"repos/{SLUG}":
            return {"private": False, "default_branch": "main"}
        if base.startswith(f"repos/{SLUG}/branches/"):
            return HEAD
        if base == f"repos/{SLUG}/rulesets":
            return []
        if base.endswith("/secrets"):
            return {"secrets": []}
        if base.startswith(f"repos/{SLUG}/git/trees/"):
            blobs = [{"path": p, "type": "blob", "sha": "0"} for p in sorted(self.tree)]
            return {"tree": blobs, "truncated": False}
        if base.startswith(f"repos/{SLUG}/contents/"):
            name = base.split("/contents/", 1)[1]
            if name in self.tree:
                return {"encoding": "base64", "content": base64.b64encode(b"x\n").decode()}
            if ok404:
                return None
        raise AssertionError(f"unexpected gh read: {path}")


class AuditRepoAlternativesCase(unittest.TestCase):
    """audit_repo's baseline loop passes each path's declared alternatives into the shared reader.

    The reader's own handling of alternatives is covered by the engine's --selftest, so what this
    adds is the call site. Were it to stop passing them, a repository holding only an alternative
    would read as missing the declared path while that self-test still passed.
    """

    def run_audit(self, tree: set[str]) -> tuple[list[tuple[str, str]], FakeGh]:
        """Audit a Python repository carrying `tree`, where a test module makes codecov.yml owed.

        The hub's tracked paths come from a fetch of its origin. Answering them as empty keeps the
        run offline and leaves the checkout the suite runs in untouched.
        """
        spec = {
            "registry": {"repos": [ENTRY], "defaults": {}},
            "settings": audit.load("repo-config/settings.json"),
            "secrets": audit.load("spec/secrets.json"),
            "files": {"baseline": [CODECOV]},
        }
        fake = FakeGh(tree)
        with (
            mock.patch.object(audit, "gh", fake),
            mock.patch.object(audit, "hub_tracked", lambda rev=None: frozenset()),
        ):
            findings, _ = audit.audit_repo(ENTRY, spec)
        return findings, fake

    def test_a_repository_holding_only_the_alternative_carries_the_file(self) -> None:
        findings, fake = self.run_audit(CARRIED | {".codecov.yml"})

        self.assertIn(f"repos/{SLUG}/contents/.codecov.yml?ref=main", fake.paths)
        absent = [text for _, text in findings if text.startswith("file: codecov.yml")]
        self.assertEqual(absent, [])

    def test_a_repository_holding_neither_is_reported_under_both_names(self) -> None:
        """The control, showing the loop under test ran and reports an absence it sees."""
        findings, _ = self.run_audit(CARRIED)

        self.assertIn(
            (
                "LETTER",
                (
                    "file: codecov.yml or .codecov.yml absent on main "
                    "(verify intent per AUDIT.md section 7)"
                ),
            ),
            findings,
        )


if __name__ == "__main__":
    unittest.main()

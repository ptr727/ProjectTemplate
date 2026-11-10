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
HEAD_SHA = "c0ffee"
HEAD = {"commit": {"sha": HEAD_SHA, "commit": {"tree": {"sha": "feed"}}}}
CODECOV = {
    "path": "codecov.yml",
    "alternatives": [".codecov.yml"],
    "appliesTo": ["python"],
}


class FakeGh:
    """The REST reads audit_repo makes, answered for a repository carrying exactly `tree`.

    An unanticipated read fails the test rather than answering. A guessed answer could steer
    audit_repo past the loop under test while still reading as a pass. A path in `moved` is one a
    push deleted after the tree was listed, so it resolves at the head commit and nowhere else.
    """

    def __init__(
        self,
        tree: set[str],
        modes: dict[str, str] | None = None,
        unread: frozenset[str] = frozenset(),
        texts: dict[str, str] | None = None,
        moved: frozenset[str] = frozenset(),
    ) -> None:
        self.tree = tree
        self.modes = modes or {}
        self.unread = unread
        self.texts = texts or {}
        self.moved = moved
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
            blobs = [
                {"path": p, "type": "blob", "sha": "0", "mode": self.modes.get(p, "100644")}
                for p in sorted(self.tree)
            ]
            return {"tree": blobs, "truncated": False}
        if base.startswith(f"repos/{SLUG}/contents/"):
            name = base.split("/contents/", 1)[1]
            if name in self.unread or (name in self.moved and path != f"{base}?ref={HEAD_SHA}"):
                if ok404:
                    return None
                raise RuntimeError(f"gh api {path}: HTTP 404")
            if name in self.tree:
                text = self.texts.get(name, "x\n").encode()
                return {"encoding": "base64", "content": base64.b64encode(text).decode()}
            if ok404:
                return None
        raise AssertionError(f"unexpected gh read: {path}")


def run_audit(
    tree: set[str],
    modes: dict[str, str] | None = None,
    unread: frozenset[str] = frozenset(),
    hub: frozenset[str] = frozenset(),
    texts: dict[str, str] | None = None,
    moved: frozenset[str] = frozenset(),
) -> tuple[list[tuple[str, str]], FakeGh]:
    """Audit a Python repository carrying `tree`, where a test module makes codecov.yml owed.

    The hub's tracked paths come from a fetch of its origin. Answering them with `hub` keeps the
    run offline and leaves the checkout the suite runs in untouched. The repo-owned trees and the
    gap ledger are the shipped declarations, so a test reads the exclusions the real audit applies.
    """
    spec = {
        "registry": {"repos": [ENTRY], "defaults": {}},
        "settings": audit.load("repo-config/settings.json"),
        "secrets": audit.load("spec/secrets.json"),
        "files": {
            "baseline": [CODECOV],
            "repoOwnedTrees": audit.load("spec/files.json")["repoOwnedTrees"],
        },
        "divergences": audit.load("spec/divergences.json"),
    }
    fake = FakeGh(tree, modes, unread, texts, moved)
    with (
        mock.patch.object(audit, "gh", fake),
        mock.patch.object(audit, "hub_tracked", lambda rev=None: hub),
    ):
        findings, _ = audit.audit_repo(ENTRY, spec)
    return findings, fake


class AuditRepoAlternativesCase(unittest.TestCase):
    """audit_repo's baseline loop passes each path's declared alternatives into the shared reader.

    The reader's own handling of alternatives is covered by tests/test_audit_helpers.py, so what this
    adds is the call site. Were it to stop passing them, a repository holding only an alternative
    would read as missing the declared path while that self-test still passed.
    """

    def test_a_repository_holding_only_the_alternative_carries_the_file(self) -> None:
        findings, fake = run_audit(CARRIED | {".codecov.yml"})

        self.assertIn(f"repos/{SLUG}/contents/.codecov.yml?ref={HEAD_SHA}", fake.paths)
        absent = [text for _, text in findings if text.startswith("file: codecov.yml")]
        self.assertEqual(absent, [])

    def test_a_repository_holding_neither_is_reported_under_both_names(self) -> None:
        """The control, showing the loop under test ran and reports an absence it sees."""
        findings, _ = run_audit(CARRIED)

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


class AuditRepoNamingCase(unittest.TestCase):
    """audit_repo hands the tree it read, modes included, to the file-naming and secret-stem checks.

    Each check's own cases are covered by tests/test_audit_helpers.py, so what this adds is the call
    site: the modes reaching the file-naming check, and its shebang read going to the audited head.
    """

    def naming_findings(self, findings: list[tuple[str, str]]) -> list[str]:
        return [
            text for _, text in findings if text.startswith(("file-naming:", "runtime-secrets:"))
        ]

    def test_a_conforming_tree_reports_no_naming_finding(self) -> None:
        tree = CARRIED | {
            ".codecov.yml",
            "scripts/sync-tools.sh",
            ".secrets/db_root_password.example",
        }
        findings, fake = run_audit(tree, {"scripts/sync-tools.sh": "100755"})

        self.assertEqual(self.naming_findings(findings), [])
        self.assertFalse([p for p in fake.paths if "/contents/scripts/" in p])

    def test_each_non_conforming_shape_is_reported(self) -> None:
        tree = CARRIED | {
            ".codecov.yml",
            "scripts/Fetch-Report.py",
            "bin/Run_Backup",
            ".secrets/Db_Root_Password.example",
        }
        findings, fake = run_audit(tree, {"bin/Run_Backup": "100755"})

        self.assertIn(f"repos/{SLUG}/contents/bin/Run_Backup?ref={HEAD_SHA}", fake.paths)
        self.assertEqual(
            self.naming_findings(findings),
            [
                (
                    'file-naming: 1 file(s) break CODESTYLE.md "Script and Utility File Names". '
                    "Rename each, or where a tool or platform fixes the name, record a driftNote "
                    "naming (filenaming.scripts) and the path as a code span: scripts/Fetch-Report.py"
                ),
                (
                    "file-naming: 1 file(s) the per-extension patterns cannot decide, judge each by "
                    'hand against CODESTYLE.md "Script and Utility File Names": bin/Run_Backup '
                    "(an executable with no shebang)"
                ),
                (
                    "runtime-secrets: 1 env or secret file(s) break the env and secret file rule "
                    'of CODESTYLE.md "Script and Utility File Names", or are a .txt or bare .env '
                    'file in a secrets directory, which GOVERNANCE.md "Repo-Scoped Secrets" rules '
                    "out: .secrets/Db_Root_Password.example"
                ),
            ],
        )

    def test_a_bare_secrets_directory_is_read_from_the_tracked_files_naming_it(self) -> None:
        """The issue's constructed case: nothing under secrets/ is tracked, so the audited head's files are read."""
        texts = {
            ".gitignore": "secrets/\n",
            ".vscode/launch.json": (
                '{"args": ["--token-file=${workspaceFolder}/secrets/app_token.txt"]}\n'
            ),
        }
        findings, fake = run_audit(CARRIED | {".codecov.yml"} | set(texts), texts=texts)

        self.assertIn(f"repos/{SLUG}/contents/.vscode/launch.json?ref={HEAD_SHA}", fake.paths)
        self.assertEqual(
            self.naming_findings(findings),
            [
                (
                    "runtime-secrets: 1 env or secret file(s) break the env and secret file rule "
                    'of CODESTYLE.md "Script and Utility File Names", or are a .txt or bare .env '
                    'file in a secrets directory, which GOVERNANCE.md "Repo-Scoped Secrets" rules '
                    "out: secrets/app_token.txt (named in .vscode/launch.json)"
                ),
                (
                    "runtime-secrets: 2 tracked file(s) name or sit in a bare secrets/ directory, "
                    'which GOVERNANCE.md "Repo-Scoped Secrets" rules out. Rename it .secrets/ and '
                    "update each: .gitignore, .vscode/launch.json"
                ),
            ],
        )

    def test_a_failed_shebang_read_raises_rather_than_reading_as_undecided(self) -> None:
        """The tree listed the path as a blob, so a 404 reading it is a failed read, not an absent file."""
        tree = CARRIED | {".codecov.yml", "bin/Run_Backup"}

        with self.assertRaises(RuntimeError):
            run_audit(tree, {"bin/Run_Backup": "100755"}, frozenset({"bin/Run_Backup"}))


class AuditRepoHeadCommitCase(unittest.TestCase):
    """audit_repo reads content at the head commit it listed the tree at, not at the branch name.

    A push landing between the two reads would otherwise answer a listed path with a 404. A read
    that treats a 404 as a failure would then turn the whole repository's result into one ERROR.
    """

    def test_a_path_deleted_after_the_tree_was_listed_is_still_judged(self) -> None:
        tree = CARRIED | {".codecov.yml", "bin/Run_Backup"}
        findings, fake = run_audit(
            tree, {"bin/Run_Backup": "100755"}, moved=frozenset({"bin/Run_Backup", ".codecov.yml"})
        )

        self.assertEqual([kind for kind, _ in findings if kind == "ERROR"], [])
        self.assertIn(f"repos/{SLUG}/contents/bin/Run_Backup?ref={HEAD_SHA}", fake.paths)
        self.assertTrue(
            any("bin/Run_Backup (an executable with no shebang)" in text for _, text in findings),
            findings,
        )
        self.assertEqual([text for _, text in findings if text.startswith("file: codecov.yml")], [])

    def test_every_content_read_names_the_head_commit(self) -> None:
        _, fake = run_audit(CARRIED | {".codecov.yml", "cspell.json", ".github/dependabot.yml"})

        contents = [p for p in fake.paths if "/contents/" in p or "/commits?" in p]
        self.assertTrue(contents)
        self.assertEqual(
            [
                p
                for p in contents
                if not p.endswith((f"ref={HEAD_SHA}", f"sha={HEAD_SHA}&per_page=1"))
            ],
            [],
        )


class AuditRepoHubOnlyCase(unittest.TestCase):
    """audit_repo leaves a carrier's own test module out of the hub-only check.

    The hub's tests are never carried, so a carrier's file at a hub test path is a collision.
    Reporting it would ask for the deletion of that repo's own suite.
    """

    HUB = frozenset({"tests/test_a.py", "tools/hub_tool.py"})

    def hub_only_findings(self, findings: list[tuple[str, str]]) -> list[str]:
        return [text for _, text in findings if text.startswith("hub-only:")]

    def test_a_test_module_sharing_a_hub_test_name_is_not_reported(self) -> None:
        findings, _ = run_audit(CARRIED | {".codecov.yml"}, hub=self.HUB)

        self.assertEqual(self.hub_only_findings(findings), [])

    def test_a_hub_only_path_outside_tests_is_still_reported(self) -> None:
        """The control, showing the check under test ran and reports a hub-only path it sees."""
        findings, _ = run_audit(CARRIED | {".codecov.yml", "tools/hub_tool.py"}, hub=self.HUB)

        hub_only = self.hub_only_findings(findings)
        self.assertTrue(
            any(text.startswith("hub-only: tools/hub_tool.py ") for text in hub_only), hub_only
        )

    def test_the_rendered_reviewer_configs_are_not_reported(self) -> None:
        """The hub renders these per repository and repo-config/configure.sh check judges them."""
        rendered = {".coderabbit.yaml", ".github/agent-policy.json", ".pr_agent.toml"}
        findings, _ = run_audit(
            CARRIED | {".codecov.yml"} | rendered, hub=self.HUB | frozenset(rendered)
        )

        self.assertEqual(self.hub_only_findings(findings), [])


if __name__ == "__main__":
    unittest.main()

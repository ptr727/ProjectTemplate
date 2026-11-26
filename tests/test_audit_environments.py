#!/usr/bin/env python3
"""spec/audit_environments.py's environment-store check on constructed fixtures, with every read stubbed."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import unittest
from collections.abc import Mapping
from typing import Any
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "spec"))
import audit
import audit_environments

SLUG = "acme/Widget"
Live = dict[str, dict[str, list[str]]]


def reader(live: Mapping[str, Mapping[str, list[str]]], paths: list[str] | None = None) -> Any:
    """A names reader answering `live`, keyed by environment and then by store, recording each path read."""

    def read(path: str, key: str) -> list[str]:
        if paths is not None:
            paths.append(path)
        route = path.partition("?")[0].removeprefix(f"repos/{SLUG}/environments")
        if route == "":
            return list(live)
        env, _, store = route.removeprefix("/").rpartition("/")
        assert store == key, path
        return list(live[next(e for e in live if e.replace(" ", "%20") == env)].get(key, []))

    return read


def findings(entry: Mapping[str, Any], live: Mapping[str, Mapping[str, list[str]]]) -> list[Any]:
    return audit_environments.environment_findings(SLUG, entry, reader(live))


def entry(*envs: dict[str, Any]) -> dict[str, Any]:
    return {"name": "Widget", "environments": list(envs)}


STAGING = {
    "name": "staging",
    "branchPolicy": "none",
    "secrets": ["DEPLOY_KEY"],
    "variables": ["SITE_URL"],
}


class CompareCase(unittest.TestCase):
    """Each kind of finding, from a declaration and a live store built for it."""

    def test_a_store_matching_its_declaration_is_clean(self) -> None:
        live = {"staging": {"secrets": ["DEPLOY_KEY"], "variables": ["SITE_URL"]}}
        self.assertEqual(findings(entry(STAGING), live), [])

    def test_a_declared_name_the_store_lacks_is_a_defect(self) -> None:
        live = {"staging": {"secrets": [], "variables": ["SITE_URL"]}}
        self.assertEqual(
            findings(entry(STAGING), live),
            [("DEFECT", "secrets: secret DEPLOY_KEY missing from environment staging")],
        )

    def test_a_held_name_the_declaration_omits_is_drift(self) -> None:
        live = {"staging": {"secrets": ["DEPLOY_KEY", "OLD_TOKEN"], "variables": ["SITE_URL"]}}
        self.assertEqual(
            findings(entry(STAGING), live),
            [
                (
                    "DRIFT",
                    (
                        "secrets: secret OLD_TOKEN in environment staging is not in the registry's "
                        "declaration for it (stale?)"
                    ),
                )
            ],
        )

    def test_a_name_held_as_the_other_kind_is_one_defect(self) -> None:
        live = {"staging": {"secrets": [], "variables": ["SITE_URL", "DEPLOY_KEY"]}}
        self.assertEqual(
            findings(entry(STAGING), live),
            [
                (
                    "DEFECT",
                    "secrets: DEPLOY_KEY in environment staging is held as a variable but declared as a secret",
                )
            ],
        )

    def test_one_name_declared_and_held_as_both_kinds_is_clean(self) -> None:
        both = {
            "name": "staging",
            "branchPolicy": "none",
            "secrets": ["SITE"],
            "variables": ["SITE"],
        }
        self.assertEqual(
            findings(entry(both), {"staging": {"secrets": ["SITE"], "variables": ["SITE"]}}), []
        )

    def test_an_absent_list_declares_an_empty_store(self) -> None:
        bare = {"name": "pypi", "branchPolicy": "none"}
        live = {"pypi": {"secrets": [], "variables": ["INDEX_URL"]}}
        self.assertEqual(
            findings(entry(bare), live),
            [
                (
                    "DRIFT",
                    (
                        "secrets: variable INDEX_URL in environment pypi is not in the registry's "
                        "declaration for it (stale?)"
                    ),
                )
            ],
        )

    def test_an_undeclared_environment_must_hold_nothing(self) -> None:
        live: Live = {
            "staging": {"secrets": ["DEPLOY_KEY"], "variables": ["SITE_URL"]},
            "preview": {"secrets": ["DEPLOY_KEY"], "variables": []},
            "empty": {"secrets": [], "variables": []},
        }
        self.assertEqual(
            findings(entry(STAGING), live),
            [
                (
                    "DRIFT",
                    (
                        "secrets: secret DEPLOY_KEY in environment preview, which the registry does not "
                        "declare, so it must hold nothing"
                    ),
                )
            ],
        )

    def test_a_repo_declaring_no_environment_has_each_live_one_read(self) -> None:
        live = {"preview": {"secrets": [], "variables": ["SITE_URL"]}}
        self.assertEqual(len(findings({"name": "Widget"}, live)), 1)

    def test_a_declared_environment_github_lacks_misses_every_name(self) -> None:
        self.assertEqual(
            findings(entry(STAGING), {}),
            [
                (
                    "DEFECT",
                    "secrets: secret DEPLOY_KEY missing from environment staging (the environment does not exist)",
                ),
                (
                    "DEFECT",
                    "secrets: variable SITE_URL missing from environment staging (the environment does not exist)",
                ),
            ],
        )


class ReadCase(unittest.TestCase):
    """What is read, and how a failed read ends."""

    def test_each_store_of_each_live_environment_is_read_with_its_page_size(self) -> None:
        paths: list[str] = []
        live: Live = {"blue green": {"secrets": [], "variables": []}}
        audit_environments.environment_findings(SLUG, entry(), reader(live, paths))
        self.assertEqual(
            paths,
            [
                f"repos/{SLUG}/environments?per_page=100",
                f"repos/{SLUG}/environments/blue%20green/secrets?per_page=100",
                f"repos/{SLUG}/environments/blue%20green/variables?per_page=30",
            ],
        )

    def test_names_projects_to_names_inside_gh_on_a_pinned_host(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="ALPHA\nBETA\n", stderr="")
        with mock.patch.object(audit_environments.subprocess, "run", return_value=done) as run:
            self.assertEqual(
                audit_environments.names("repos/acme/Widget/environments", "environments"),
                ["ALPHA", "BETA"],
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], ["gh", "api", "--paginate", "--hostname", "github.com"])
        self.assertEqual(argv[-2:], ["--jq", ".environments[].name"])

    def test_a_failed_read_raises_rather_than_answering_empty(self) -> None:
        failed = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="gh: Must have admin rights (HTTP 403)\n"
        )
        with (
            mock.patch.object(audit_environments.subprocess, "run", return_value=failed),
            self.assertRaisesRegex(RuntimeError, r"HTTP 403"),
        ):
            audit_environments.names("repos/acme/Widget/environments", "environments")

    def test_the_audit_check_reads_through_the_module_reader(self) -> None:
        """audit.check_secrets_environments calls the module, so replacing its reader reaches the check."""
        run = audit.Run(entry(STAGING), {}, SLUG, "main", {}, None, None)
        live = {"staging": {"secrets": ["DEPLOY_KEY"], "variables": []}}
        with mock.patch.object(audit_environments, "names", reader(live)):
            self.assertEqual(
                audit.check_secrets_environments(run),
                [("DEFECT", "secrets: variable SITE_URL missing from environment staging")],
            )

    def test_a_failed_read_makes_the_run_an_error(self) -> None:
        """The check lets the raise through, which audit.main then reports as the repo's ERROR rather than a pass."""

        def fail(path: str, key: str) -> list[str]:
            raise RuntimeError(f"gh api {path}: HTTP 403")

        run = audit.Run(entry(STAGING), {}, SLUG, "main", {}, None, None)
        with (
            mock.patch.object(audit_environments, "names", fail),
            self.assertRaisesRegex(RuntimeError, "HTTP 403"),
        ):
            audit.check_secrets_environments(run)


class RegistryCase(unittest.TestCase):
    """The shipped registry's declarations take the shape the check reads."""

    def test_every_declared_environment_resolves_to_two_name_sets(self) -> None:
        for repo in audit.load("registry/repos.json")["repos"]:
            with self.subTest(repo=repo["name"]):
                for stores in audit_environments.declared_stores(repo).values():
                    self.assertEqual(set(stores), {"secrets", "variables"})


if __name__ == "__main__":
    unittest.main()

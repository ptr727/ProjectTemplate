#!/usr/bin/env python3
"""Exercise the secret name resolver, its CLI, and the scaffold command that pre-creates the host files.

Every value here is synthetic, and every repo name is invented.
The scaffold runs against a temporary directory with the tree reads stubbed.
So nothing reaches GitHub and no real secrets directory is touched.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB / "spec"))
import audit
import resolve_secrets
import secret_names
from import_probe import import_delta

SECRETS: dict[str, Any] = json.loads((HUB / "spec" / "secrets.json").read_text(encoding="utf-8"))

TESTED = {"tests/test_a.py", "pyproject.toml"}
UNTESTED = {"src/pkg/__init__.py", "pyproject.toml"}


def entry(**fields: Any) -> dict[str, Any]:
    return {"name": "ExampleRepo", "types": ["python"], **fields}


class SecretFileNameCase(unittest.TestCase):
    """The single place a host file name is made."""

    def test_a_fleet_name_is_github_then_the_lower_case_name(self) -> None:
        self.assertEqual(
            secret_names.secret_file_name("DOCKER_HUB_USERNAME", "fleet", "Any-Repo"),
            "github_docker_hub_username",
        )

    def test_a_repository_name_adds_the_lower_case_repo_with_hyphens_kept(self) -> None:
        self.assertEqual(
            secret_names.secret_file_name("CODECOV_TOKEN", "repository", "Example-Repo"),
            "github_example-repo_codecov_token",
        )

    def test_an_unknown_scope_raises(self) -> None:
        with self.assertRaises(secret_names.SecretNameError):
            secret_names.secret_file_name("CODECOV_TOKEN", "environment", "ExampleRepo")

    def test_the_wrong_scope_file_is_the_other_scope_spelling(self) -> None:
        self.assertEqual(
            secret_names.wrong_scope_file_name("CODECOV_TOKEN", "repository", "ExampleRepo"),
            "github_codecov_token",
        )
        self.assertEqual(
            secret_names.wrong_scope_file_name("NUGET_USERNAME", "fleet", "ExampleRepo"),
            "github_examplerepo_nuget_username",
        )


class ResolveSecretStoresCase(unittest.TestCase):
    """The names each store must hold, as spec/audit.py's check consumes them."""

    def stores(self, e: dict[str, Any], tree: set[str] | None) -> dict[str, set[str]]:
        return secret_names.resolve_secret_stores(SECRETS, e, tree)[0]

    BASELINE: ClassVar[set[str]] = {"CODEGEN_APP_CLIENT_ID", "CODEGEN_APP_PRIVATE_KEY"}

    def test_the_baseline_alone_in_both_stores(self) -> None:
        required = self.stores(entry(types=["docs"]), UNTESTED)
        self.assertEqual(required, {"actions": self.BASELINE, "dependabot": self.BASELINE})

    def test_a_docker_target_adds_the_docker_hub_pair_in_both_stores(self) -> None:
        required = self.stores(entry(types=["docs"], publish=[{"target": "docker"}]), UNTESTED)
        docker = {"DOCKER_HUB_USERNAME", "DOCKER_HUB_ACCESS_TOKEN"}
        self.assertEqual(required["actions"], self.BASELINE | docker)
        self.assertEqual(required["dependabot"], self.BASELINE | docker)

    def test_codecov_is_owed_with_a_test_tree_and_not_without_one(self) -> None:
        with_tests = self.stores(entry(), TESTED)
        self.assertIn("CODECOV_TOKEN", with_tests["actions"])
        self.assertIn("CODECOV_TOKEN", with_tests["dependabot"])
        without = self.stores(entry(), UNTESTED)
        self.assertNotIn("CODECOV_TOKEN", without["actions"] | without["dependabot"])

    def test_an_unread_tree_keeps_the_codecov_claim(self) -> None:
        self.assertIn("CODECOV_TOKEN", self.stores(entry(), None)["actions"])

    def test_required_secrets_are_added_to_the_actions_store_only(self) -> None:
        required = self.stores(entry(types=["docs"], requiredSecrets=["NUGET_USERNAME"]), None)
        self.assertIn("NUGET_USERNAME", required["actions"])
        self.assertNotIn("NUGET_USERNAME", required["dependabot"])

    def test_the_forbidden_names_follow_the_claimed_mechanisms(self) -> None:
        _, forbidden = secret_names.resolve_secret_stores(
            SECRETS, entry(types=["docs"], publish=[{"target": "nuget"}]), None
        )
        self.assertEqual(forbidden, {"CODEGEN_APP_ID", "NUGET_API_KEY"})


class AuditAgreementCase(unittest.TestCase):
    """The audit's secret check and the provisioning plan are one resolver, so they cannot disagree."""

    def audit_missing(self, e: dict[str, Any], tree: set[str]) -> dict[str, set[str]]:
        run = SimpleNamespace(
            spec={"secrets": SECRETS}, entry=e, tree=tree, slug="owner/ExampleRepo"
        )
        with unittest.mock.patch.object(audit, "gh", return_value={"secrets": []}):
            findings = audit.check_secrets_names(run)
        missing: dict[str, set[str]] = {"actions": set(), "dependabot": set()}
        for severity, text in findings:
            self.assertEqual(severity, "DEFECT")
            name, _, rest = text.removeprefix("secrets: ").partition(" missing from the ")
            missing[rest.removesuffix(" store")].add(name)
        return missing

    def test_an_empty_store_reports_exactly_the_names_the_plan_requires(self) -> None:
        cases = [
            (entry(types=["docs"]), UNTESTED),
            (entry(types=["docs"], publish=[{"target": "docker"}]), UNTESTED),
            (entry(), TESTED),
            (entry(), UNTESTED),
            (entry(types=["docs"], requiredSecrets=["NUGET_USERNAME"]), UNTESTED),
        ]
        for e, tree in cases:
            with self.subTest(types=e["types"], tree=sorted(tree)):
                plan = secret_names.secret_plan(SECRETS, e, tree, "ExampleRepo")
                from_plan: dict[str, set[str]] = {"actions": set(), "dependabot": set()}
                for row in plan["rows"]:
                    for store in row["stores"]:
                        from_plan[store].add(row["name"])
                self.assertEqual(self.audit_missing(e, tree), from_plan)

    def test_the_names_moved_out_of_audit_are_still_importable_from_it(self) -> None:
        for moved in (
            "in_test_directory",
            "is_python_test_module",
            "is_csharp_test_project",
            "coverage_candidate_types",
            "coverage_claiming_types",
            "declared_types",
            "resolve_secret_stores",
        ):
            with self.subTest(moved=moved):
                self.assertIs(getattr(audit, moved), getattr(secret_names, moved))


class SecretPlanCase(unittest.TestCase):
    """The rows the script consumes, and the wrong-scope files it refuses to run beside."""

    def test_each_row_names_its_scope_file_and_stores(self) -> None:
        plan = secret_names.secret_plan(SECRETS, entry(), TESTED, "ExampleRepo")
        rows = {r["name"]: r for r in plan["rows"]}
        self.assertEqual(
            rows["CODECOV_TOKEN"],
            {
                "name": "CODECOV_TOKEN",
                "scope": "repository",
                "file": "github_examplerepo_codecov_token",
                "stores": ["actions", "dependabot"],
            },
        )
        self.assertEqual(rows["CODEGEN_APP_CLIENT_ID"]["file"], "github_codegen_app_client_id")
        self.assertEqual(rows["CODEGEN_APP_CLIENT_ID"]["scope"], "fleet")

    def test_the_strays_are_each_name_at_the_other_scope(self) -> None:
        plan = secret_names.secret_plan(SECRETS, entry(), TESTED, "ExampleRepo")
        self.assertIn("github_codecov_token", plan["strays"])
        self.assertIn("github_examplerepo_codegen_app_client_id", plan["strays"])
        self.assertFalse(set(plan["strays"]) & {r["file"] for r in plan["rows"]})

    def test_a_name_without_a_scope_raises(self) -> None:
        unscoped = {**SECRETS, "secretScopes": {"CODEGEN_APP_CLIENT_ID": "fleet"}}
        with self.assertRaises(secret_names.SecretNameError):
            secret_names.secret_plan(unscoped, entry(), TESTED, "ExampleRepo")

    def test_a_missing_scope_table_raises(self) -> None:
        without = {k: v for k, v in SECRETS.items() if k != "secretScopes"}
        with self.assertRaises(secret_names.SecretNameError):
            secret_names.secret_plan(without, entry(), TESTED, "ExampleRepo")


class NamesCliCase(unittest.TestCase):
    """`resolve_secrets.py names`, run as configure.sh runs it."""

    def run_names(
        self, registry: Any, name: str, stdin: bytes = b"", secrets: Any = None
    ) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "repos.json"
            reg.write_text(json.dumps(registry), encoding="utf-8")
            spec = Path(tmp) / "secrets.json"
            spec.write_text(json.dumps(SECRETS if secrets is None else secrets), encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    str(HUB / "spec" / "resolve_secrets.py"),
                    "names",
                    "--",
                    str(spec),
                    str(reg),
                    name,
                ],
                input=stdin,
                capture_output=True,
                check=False,
                timeout=30,
            )

    def test_blob_paths_on_stdin_decide_codecov(self) -> None:
        registry = {"repos": [entry()]}
        with_tests = self.run_names(registry, "ExampleRepo", b"tests/test_a.py\0")
        self.assertEqual(with_tests.returncode, 0, with_tests.stderr)
        self.assertIn("CODECOV_TOKEN", [r["name"] for r in json.loads(with_tests.stdout)["rows"]])
        without = self.run_names(registry, "ExampleRepo", b"src/a.py\0")
        self.assertNotIn("CODECOV_TOKEN", [r["name"] for r in json.loads(without.stdout)["rows"]])

    def test_a_path_holding_a_newline_stays_one_path(self) -> None:
        result = self.run_names({"repos": [entry()]}, "ExampleRepo", b"tests/odd\nname_test.py\0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CODECOV_TOKEN", [r["name"] for r in json.loads(result.stdout)["rows"]])

    def test_each_loud_failure_exits_one_with_a_message(self) -> None:
        cases: list[tuple[str, Any, str, Any]] = [
            ("a registry with no repos array", {}, "ExampleRepo", None),
            ("an unknown entry", {"repos": [entry()]}, "OtherRepo", None),
            ("a duplicate entry", {"repos": [entry(), entry()]}, "ExampleRepo", None),
            (
                "a name with no scope",
                {"repos": [entry()]},
                "ExampleRepo",
                {**SECRETS, "secretScopes": {}},
            ),
        ]
        for label, registry, name, secrets in cases:
            with self.subTest(label):
                result = self.run_names(registry, name, b"tests/test_a.py\0", secrets)
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr.strip())
                self.assertNotIn(b"Traceback", result.stderr)
                self.assertEqual(result.stdout, b"")


def tree_reader(trees: dict[str, set[str]]):
    def read(slug: str) -> set[str]:
        if slug not in trees:
            raise resolve_secrets.ResolveError(f"{slug}: the tree could not be read")
        return trees[slug]

    return read


FLEET = {
    "repos": [
        {
            "name": "ExampleRepo",
            "url": "https://github.com/owner/ExampleRepo",
            "types": ["python"],
            "publish": [{"target": "docker"}],
        },
        {
            "name": "Other-Repo",
            "url": "https://github.com/owner/Other-Repo",
            "types": ["python"],
        },
        {
            "name": "OldRepo",
            "url": "https://github.com/owner/OldRepo",
            "status": "archived",
            "types": ["python"],
        },
    ]
}
TREES = {"owner/ExampleRepo": TESTED, "owner/Other-Repo": UNTESTED}


class ScaffoldCase(unittest.TestCase):
    """The scaffold creates only what is missing, in a private directory, and never touches what exists."""

    def run_scaffold(
        self, directory: Path, dry_run: bool = False, fleet: Any = FLEET, trees: Any = TREES
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        return resolve_secrets.scaffold(SECRETS, fleet, directory, dry_run, tree_reader(trees))

    def test_it_creates_the_directory_private_and_each_file_empty_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".secrets"
            created, present, strays, unclaimed = self.run_scaffold(directory)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(sorted(p.name for p in directory.iterdir()), created)
            for path in directory.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
                self.assertEqual(path.stat().st_size, 0, path.name)
            self.assertEqual((present, strays, unclaimed), ([], [], []))
            # Fleet names once, repository names per repo, and codecov only where tests exist.
            self.assertIn("github_codegen_app_client_id", created)
            self.assertIn("github_docker_hub_username", created)
            self.assertIn("github_examplerepo_codecov_token", created)
            self.assertNotIn("github_other-repo_codecov_token", created)
            self.assertFalse([c for c in created if "oldrepo" in c])
            self.assertEqual(len(created), len(set(created)))

    def test_an_existing_file_keeps_its_content_mode_and_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            kept = directory / "github_codegen_app_client_id"
            kept.write_text("sentinel-kept-value-1\n", encoding="utf-8")
            kept.chmod(0o640)
            os.utime(kept, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
            before = kept.stat()
            created, present, _, _ = self.run_scaffold(directory)
            after = kept.stat()
            self.assertEqual(kept.read_text(encoding="utf-8"), "sentinel-kept-value-1\n")
            self.assertEqual(
                (before.st_mode, before.st_mtime_ns, before.st_size),
                (after.st_mode, after.st_mtime_ns, after.st_size),
            )
            self.assertIn("github_codegen_app_client_id", present)
            self.assertNotIn("github_codegen_app_client_id", created)

    def test_a_directory_open_to_group_or_other_is_refused_with_nothing_created(self) -> None:
        for mode in (0o750, 0o705, 0o755):
            with self.subTest(mode=oct(mode)), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                directory.chmod(mode)
                with self.assertRaises(resolve_secrets.ResolveError):
                    self.run_scaffold(directory)
                self.assertEqual(list(directory.iterdir()), [])

    def test_a_wrong_scope_file_stops_the_run_before_anything_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            stray = directory / "github_codecov_token"
            stray.write_text("sentinel-stray-value-1\n", encoding="utf-8")
            other = directory / "github_other-repo_codegen_app_client_id"
            other.write_text("sentinel-stray-value-2\n", encoding="utf-8")
            # A stray nothing opens stays reported even where its mode would refuse a read.
            other.chmod(0o000)
            created, _, strays, _ = self.run_scaffold(directory)
            self.assertEqual(created, [])
            self.assertEqual(
                strays, ["github_codecov_token", "github_other-repo_codegen_app_client_id"]
            )
            self.assertEqual(
                sorted(p.name for p in directory.iterdir()),
                ["github_codecov_token", "github_other-repo_codegen_app_client_id"],
            )
            self.assertEqual(stray.read_text(encoding="utf-8"), "sentinel-stray-value-1\n")

    def test_a_file_nothing_expects_is_listed_as_unclaimed_and_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            (directory / "github_oldrepo_codecov_token").write_text("", encoding="utf-8")
            (directory / "notes.txt").write_text("not a secret file\n", encoding="utf-8")
            created, _, strays, unclaimed = self.run_scaffold(directory)
            self.assertEqual(strays, [])
            self.assertEqual(unclaimed, ["github_oldrepo_codecov_token"])
            self.assertTrue((directory / "github_oldrepo_codecov_token").exists())
            self.assertTrue(created)

    def test_a_dry_run_creates_nothing_even_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".secrets"
            created, _, _, _ = self.run_scaffold(directory, dry_run=True)
            self.assertTrue(created)
            self.assertFalse(directory.exists())

    def test_an_unreadable_tree_fails_loud_naming_the_repo_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            with self.assertRaisesRegex(resolve_secrets.ResolveError, "owner/Other-Repo"):
                self.run_scaffold(directory, trees={"owner/ExampleRepo": TESTED})
            self.assertEqual(list(directory.iterdir()), [])

    def test_a_second_run_creates_nothing_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".secrets"
            first, _, _, _ = self.run_scaffold(directory)
            created, present, _, _ = self.run_scaffold(directory)
            self.assertEqual(created, [])
            self.assertEqual(present, first)


class ScaffoldCliCase(unittest.TestCase):
    """The command line around the scaffold: lists, exit status, and what it refuses to print."""

    def run_cli(self, *args: str, trees: Any = TREES) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "spec").mkdir()
            (Path(root) / "registry").mkdir()
            (Path(root) / "spec" / "secrets.json").write_text(json.dumps(SECRETS), encoding="utf-8")
            (Path(root) / "registry" / "repos.json").write_text(json.dumps(FLEET), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with (
                unittest.mock.patch.object(resolve_secrets, "ROOT", Path(root)),
                unittest.mock.patch.object(resolve_secrets, "read_tree", tree_reader(trees)),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = resolve_secrets.main(list(args))
            return code, out.getvalue(), err.getvalue()

    def test_it_prints_the_created_and_present_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".secrets"
            code, out, _ = self.run_cli("scaffold", "--secrets-dir", str(directory))
            self.assertEqual(code, 0)
            self.assertIn("Created (", out)
            self.assertIn("  github_examplerepo_codecov_token", out)
            self.assertIn("Already present, not opened (0):", out)
            code, out, _ = self.run_cli("scaffold", "--secrets-dir", str(directory))
            self.assertEqual(code, 0)
            self.assertIn("Created (0):", out)

    def test_a_stray_exits_one_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            (directory / "github_codecov_token").write_text("", encoding="utf-8")
            code, out, err = self.run_cli("scaffold", "--secrets-dir", str(directory))
            self.assertEqual(code, 1)
            self.assertIn("github_codecov_token", out)
            self.assertIn("Nothing was created", err)
            self.assertEqual([p.name for p in directory.iterdir()], ["github_codecov_token"])

    def test_a_tree_failure_exits_one_and_names_the_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli("scaffold", "--secrets-dir", str(Path(tmp) / "s"), trees={})
            self.assertEqual(code, 1)
            self.assertIn("owner/ExampleRepo", err)
            self.assertFalse((Path(tmp) / "s").exists())


class PlainImportCase(unittest.TestCase):
    """configure.sh runs both modules under whatever plain python3 it finds, with no Markdown reader or yaml."""

    def test_neither_module_pulls_in_a_third_party_reader(self) -> None:
        for module in ("secret_names", "resolve_secrets"):
            with self.subTest(module=module):
                before, after = import_delta(HUB / "spec", module)
                if before:
                    self.skipTest(
                        f"this interpreter imports {', '.join(before)} at startup, so the import delta cannot be read here"
                    )
                self.assertEqual(after, [], f"importing {module} pulled in a Markdown reader")


if __name__ == "__main__":
    unittest.main()

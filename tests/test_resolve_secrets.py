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
    return {
        "name": "ExampleRepo",
        "url": "https://github.com/owner/ExampleRepo",
        "types": ["python"],
        **fields,
    }


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

    def test_a_repo_name_with_an_underscore_or_a_dot_has_no_repository_file(self) -> None:
        """Every `_` separates fields, so a repo name carrying one, or a dot, could not split back."""
        for repo in ("Example_Repo", "Example.Repo"):
            with self.subTest(repo=repo), self.assertRaises(secret_names.SecretNameError):
                secret_names.secret_file_name("CODECOV_TOKEN", "repository", repo)

    def test_a_fleet_name_has_no_stray_where_the_repo_cannot_form_the_field(self) -> None:
        self.assertIsNone(
            secret_names.wrong_scope_file_name("NUGET_USERNAME", "fleet", "example.github.io")
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

    def test_required_secrets_are_added_to_both_stores(self) -> None:
        required = self.stores(entry(types=["docs"], requiredSecrets=["NUGET_USERNAME"]), None)
        self.assertIn("NUGET_USERNAME", required["actions"])
        self.assertIn("NUGET_USERNAME", required["dependabot"])

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

    def test_audit_and_the_cli_get_the_same_findings_for_one_set_of_live_stores(self) -> None:
        e = entry(publish=[{"target": "docker"}])
        live = {
            "actions": {"CODEGEN_APP_CLIENT_ID", "CODEGEN_APP_ID", "LEFTOVER", "CODECOV_TOKEN"},
            "dependabot": {"CODEGEN_APP_PRIVATE_KEY", "CODECOV_TOKEN"},
        }
        run = SimpleNamespace(
            spec={"secrets": SECRETS}, entry=e, tree=TESTED, slug="owner/ExampleRepo"
        )

        def answer(path: str, ok404: bool = False) -> Any:
            store = path.split("/")[-2].split("?")[0] if "/secrets" in path else ""
            return {"secrets": [{"name": n} for n in sorted(live[store])]}

        with unittest.mock.patch.object(audit, "gh", answer):
            from_audit = audit.check_secrets_names(run)
        plan = secret_names.secret_plan(SECRETS, e, TESTED, "ExampleRepo", live)
        from_cli = [tuple(f) for f in plan["findings"] if f[0] != "OK"]
        self.assertEqual(from_audit, from_cli)
        self.assertTrue(from_cli)

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


class FakeGh:
    """Stands in for scripts/gh_graphql.run_subprocess, recording each argv and answering from what it was given.

    Nothing is spawned, so the cases below run the same on every platform.
    A spawned stub `gh` cannot, since Windows' CreateProcess finds only a `gh.exe`.
    """

    def __init__(
        self,
        tree: tuple[str, ...] = (),
        truncated: bool = False,
        tree_status: str | None = None,
        live: dict[str, tuple[str, ...]] | None = None,
        missing_gh: bool = False,
    ) -> None:
        self.tree = tree
        self.truncated = truncated
        self.tree_status = tree_status
        self.live = live or {}
        self.missing_gh = missing_gh
        self.calls: list[str] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(" ".join(argv))
        if self.missing_gh:
            raise resolve_secrets.gh_graphql_module().GraphQLError("could not run gh: not found")
        endpoint = next(a for a in argv if a.startswith("repos/"))
        if "/git/trees/" in endpoint:
            if self.tree_status is not None:
                if self.tree_status == "none":
                    return 1, "", "gh: boom\n"
                return 1, "", f"gh: error (HTTP {self.tree_status})\n"
            payload = {
                "tree": [{"type": "blob", "path": p} for p in self.tree],
                "truncated": self.truncated,
            }
            return 0, json.dumps(payload), ""
        store = endpoint.split("/")[-2]
        return 0, "".join(f"{n}\n" for n in self.live.get(store, ())), ""


class NamesCliCase(unittest.TestCase):
    """`resolve_secrets.py names`, driven through main() with gh answered in process."""

    def run_names(
        self,
        registry: Any,
        name: str,
        gh: FakeGh | None = None,
        secrets: Any = None,
        live: bool = False,
        slug: str = "owner/ExampleRepo",
    ) -> tuple[int, str, str, FakeGh]:
        gh = gh or FakeGh()
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "repos.json"
            reg.write_text(json.dumps(registry), encoding="utf-8")
            spec = Path(tmp) / "secrets.json"
            spec.write_text(json.dumps(SECRETS if secrets is None else secrets), encoding="utf-8")
            args = [
                "names",
                "--slug",
                slug,
                *(["--live"] if live else []),
                "--",
                str(spec),
                str(reg),
                name,
            ]
            out, err = io.StringIO(), io.StringIO()
            with (
                unittest.mock.patch.object(
                    resolve_secrets.gh_graphql_module(), "run_subprocess", gh
                ),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = resolve_secrets.main(args)
            return code, out.getvalue(), err.getvalue(), gh

    def test_the_tree_the_stub_serves_decides_codecov(self) -> None:
        registry = {"repos": [entry()]}
        code, out, err, _ = self.run_names(registry, "ExampleRepo", FakeGh(("tests/test_a.py",)))
        self.assertEqual(code, 0, err)
        self.assertIn("CODECOV_TOKEN", [r["name"] for r in json.loads(out)["rows"]])
        _, out, _, _ = self.run_names(registry, "ExampleRepo", FakeGh(("src/a.py",)))
        self.assertNotIn("CODECOV_TOKEN", [r["name"] for r in json.loads(out)["rows"]])

    def test_a_path_holding_a_newline_stays_one_path(self) -> None:
        code, out, err, _ = self.run_names(
            {"repos": [entry()]}, "ExampleRepo", FakeGh(("tests/odd\nname_test.py",))
        )
        self.assertEqual(code, 0, err)
        self.assertIn("CODECOV_TOKEN", [r["name"] for r in json.loads(out)["rows"]])

    def test_the_tree_is_read_on_the_ground_branch_the_audit_reads(self) -> None:
        cases: list[tuple[str, Any, str]] = [
            ("the default of main", {"repos": [entry()]}, "main"),
            (
                "the entry's own branch",
                {"repos": [entry(groundTruthBranch="trunk")]},
                "trunk",
            ),
            (
                "the registry default",
                {"defaults": {"groundTruthBranch": "develop"}, "repos": [entry()]},
                "develop",
            ),
        ]
        for label, registry, branch in cases:
            with self.subTest(label):
                code, _, err, gh = self.run_names(
                    registry, "ExampleRepo", FakeGh(("tests/test_a.py",))
                )
                self.assertEqual(code, 0, err)
                self.assertEqual(
                    gh.calls, [f"gh api repos/owner/ExampleRepo/git/trees/{branch}?recursive=1"]
                )

    def test_a_slug_the_entry_does_not_name_is_refused_before_any_tree_read(self) -> None:
        cases: list[tuple[str, dict[str, Any]]] = [
            ("another owner", entry(url="https://github.com/otherowner/ExampleRepo")),
            ("another name", entry(url="https://github.com/owner/OtherName")),
            ("no url", {k: v for k, v in entry().items() if k != "url"}),
        ]
        for label, declared in cases:
            with self.subTest(label):
                code, out, err, gh = self.run_names({"repos": [declared]}, "ExampleRepo")
                self.assertEqual(code, 1)
                self.assertIn("owner/ExampleRepo", err)
                self.assertIn("Refusing", err)
                self.assertEqual(out, "")
                self.assertEqual(gh.calls, [])

    def test_the_slug_comparison_ignores_case(self) -> None:
        code, _, err, _ = self.run_names(
            {"repos": [entry(url="https://github.com/OWNER/exampleREPO")]},
            "ExampleRepo",
            FakeGh(("tests/test_a.py",)),
        )
        self.assertEqual(code, 0, err)

    def test_each_loud_failure_exits_one_with_a_message(self) -> None:
        cases: list[tuple[str, Any, str, Any]] = [
            ("a registry with no repos array", {}, "ExampleRepo", None),
            ("an unknown entry", {"repos": [entry()]}, "OtherRepo", None),
            ("a duplicate entry", {"repos": [entry(), entry()]}, "ExampleRepo", None),
            ("a case-only near-miss", {"repos": [entry()]}, "examplerepo", None),
            (
                "an empty ground branch",
                {"repos": [entry(groundTruthBranch="")]},
                "ExampleRepo",
                None,
            ),
            (
                "a name with no scope",
                {"repos": [entry()]},
                "ExampleRepo",
                {**SECRETS, "secretScopes": {}},
            ),
        ]
        for label, registry, name, secrets in cases:
            with self.subTest(label):
                code, out, err, _ = self.run_names(
                    registry, name, FakeGh(("tests/test_a.py",)), secrets
                )
                self.assertEqual(code, 1)
                self.assertTrue(err.strip())
                self.assertNotIn("Traceback", err)
                self.assertEqual(out, "")

    def test_a_truncated_tree_fails_naming_the_repo(self) -> None:
        code, out, err, _ = self.run_names(
            {"repos": [entry()]}, "ExampleRepo", FakeGh(("tests/test_a.py",), truncated=True)
        )
        self.assertEqual(code, 1)
        self.assertIn("owner/ExampleRepo", err)
        self.assertEqual(out, "")

    def test_a_missing_ground_branch_is_a_reported_state_and_not_a_failure(self) -> None:
        for status in ("404", "409"):
            with self.subTest(status=status):
                code, out, err, _ = self.run_names(
                    {"repos": [entry()]}, "ExampleRepo", FakeGh(tree_status=status)
                )
                self.assertEqual(code, 0, err)
                plan = json.loads(out)
                self.assertEqual((plan["rows"], plan["strays"], plan["forbidden"]), ([], [], []))
                self.assertEqual(
                    plan["skip"], "the main branch of owner/ExampleRepo does not exist yet"
                )

    def test_every_other_gh_failure_still_exits_one(self) -> None:
        for status in ("403", "500", "none"):
            with self.subTest(status=status):
                code, out, err, _ = self.run_names(
                    {"repos": [entry()]}, "ExampleRepo", FakeGh(tree_status=status)
                )
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("owner/ExampleRepo", err)

    def test_a_missing_gh_is_a_clear_error_and_not_a_traceback(self) -> None:
        code, out, err, _ = self.run_names(
            {"repos": [entry()]}, "ExampleRepo", FakeGh(missing_gh=True)
        )
        self.assertEqual(code, 1)
        self.assertIn("could not run gh", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")

    def test_live_adds_the_findings_of_the_store_comparison(self) -> None:
        owed = ("CODECOV_TOKEN", "CODEGEN_APP_CLIENT_ID", "CODEGEN_APP_PRIVATE_KEY")
        gh = FakeGh(
            ("tests/test_a.py",),
            live={"actions": (*owed, "CODEGEN_APP_ID", "LEFTOVER"), "dependabot": owed[1:]},
        )
        registry = {"repos": [entry(types=["python"])]}
        code, out, err, _ = self.run_names(registry, "ExampleRepo", gh, live=True)
        self.assertEqual(code, 0, err)
        findings = [tuple(f) for f in json.loads(out)["findings"]]
        self.assertIn(("OK", "secrets: CODECOV_TOKEN is in the actions store"), findings)
        self.assertIn(
            ("DEFECT", "secrets: CODECOV_TOKEN missing from the dependabot store"), findings
        )
        self.assertIn(
            ("DEFECT", "secrets: forbidden CODEGEN_APP_ID present in the actions store"), findings
        )
        self.assertIn(
            (
                "DRIFT",
                "secrets: LEFTOVER in the actions store is claimed in that store by no applicable mechanism (stale?)",
            ),
            findings,
        )
        self.assertTrue(any("--paginate" in c for c in gh.calls))

    def test_a_skipped_branch_reads_no_store(self) -> None:
        code, out, _, gh = self.run_names(
            {"repos": [entry()]}, "ExampleRepo", FakeGh(tree_status="404"), live=True
        )
        self.assertEqual(code, 0)
        self.assertNotIn("findings", json.loads(out))
        self.assertFalse(any("--paginate" in c for c in gh.calls))


def tree_reader(
    trees: dict[str, set[str]],
    branches: list[tuple[str, str]] | None = None,
    missing: frozenset[str] = frozenset(),
):
    def read(slug: str, branch: str) -> set[str]:
        if branches is not None:
            branches.append((slug, branch))
        if slug in missing:
            raise resolve_secrets.BranchMissing(f"the {branch} branch of {slug} does not exist yet")
        if slug not in trees:
            raise resolve_secrets.ResolveError(f"{slug}: the tree could not be read")
        return trees[slug]

    return read


FLEET: dict[str, Any] = {
    "repos": [
        {
            "name": "ExampleRepo",
            "shortName": "examplerepo",
            "url": "https://github.com/owner/ExampleRepo",
            "status": "cataloged",
            "types": ["python"],
            "publish": [{"target": "docker"}],
        },
        {
            "name": "Other-Repo",
            "shortName": "otherrepo",
            "url": "https://github.com/owner/Other-Repo",
            "status": "cataloged",
            "types": ["python"],
        },
        {
            "name": "Excluded-Repo",
            "shortName": "excludedrepo",
            "url": "https://github.com/owner/Excluded-Repo",
            "status": "excluded",
            "types": ["python"],
        },
        {
            "name": "OldRepo",
            "shortName": "oldrepo",
            "url": "https://github.com/owner/OldRepo",
            "status": "archived",
            "types": ["python"],
        },
    ]
}
TREES = {"owner/ExampleRepo": TESTED, "owner/Other-Repo": UNTESTED}


# Windows keeps no POSIX permission bits, so a mode it reports back is not the one scaffold asked for.
POSIX_MODES = os.name != "nt"


class ScaffoldCase(unittest.TestCase):
    """The scaffold creates only what is missing, in a private directory, and never touches what exists."""

    def run_scaffold(
        self, directory: Path, dry_run: bool = False, fleet: Any = FLEET, trees: Any = TREES
    ) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
        return resolve_secrets.scaffold(SECRETS, fleet, directory, dry_run, tree_reader(trees))

    def test_it_creates_the_directory_private_and_each_file_empty_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".secrets"
            created, present, strays, unclaimed, _ = self.run_scaffold(directory)
            if POSIX_MODES:
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(sorted(p.name for p in directory.iterdir()), created)
            for path in directory.iterdir():
                if POSIX_MODES:
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
            created, present, _, _, _ = self.run_scaffold(directory)
            after = kept.stat()
            self.assertEqual(kept.read_text(encoding="utf-8"), "sentinel-kept-value-1\n")
            self.assertEqual(
                (before.st_mode, before.st_mtime_ns, before.st_size),
                (after.st_mode, after.st_mtime_ns, after.st_size),
            )
            self.assertIn("github_codegen_app_client_id", present)
            self.assertNotIn("github_codegen_app_client_id", created)

    @unittest.skipUnless(POSIX_MODES, "Windows keeps no POSIX permission bits to compare")
    def test_an_existing_directory_keeps_its_mode_whatever_it_is(self) -> None:
        for mode in (0o750, 0o755):
            with self.subTest(mode=oct(mode)), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                directory.chmod(mode)
                created, _, _, _, _ = self.run_scaffold(directory)
                self.assertTrue(created)
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), mode)

    def test_only_cataloged_entries_owe_files(self) -> None:
        """An excluded or archived entry is skipped, and its tree is never read."""
        branches: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            created, _, _, _, _ = resolve_secrets.scaffold(
                SECRETS, FLEET, Path(tmp) / "s", True, tree_reader(TREES, branches)
            )
        self.assertEqual(sorted(s for s, _ in branches), ["owner/ExampleRepo", "owner/Other-Repo"])
        self.assertFalse([c for c in created if "excluded" in c or "oldrepo" in c])

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
            created, _, strays, _, _ = self.run_scaffold(directory)
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
            created, _, strays, unclaimed, _ = self.run_scaffold(directory)
            self.assertEqual(strays, [])
            self.assertEqual(unclaimed, ["github_oldrepo_codecov_token", "notes.txt"])
            self.assertTrue((directory / "github_oldrepo_codecov_token").exists())
            self.assertTrue(created)

    def test_a_subdirectory_is_never_a_stray_or_unclaimed_and_is_never_created_over(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            for name in ("notes", "github_codecov_token", "github_codegen_app_client_id"):
                (directory / name).mkdir()
            created, present, strays, unclaimed, _ = self.run_scaffold(directory)
            self.assertEqual((strays, unclaimed), ([], []))
            self.assertIn("github_codegen_app_client_id", present)
            self.assertNotIn("github_codegen_app_client_id", created)
            self.assertTrue(created)

    @unittest.skipUnless(POSIX_MODES, "creating a symlink on Windows needs a privilege")
    def test_a_symlink_reads_as_a_file_at_its_name_whatever_it_resolves_to(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".secrets"
            directory.mkdir(mode=0o700)
            (Path(tmp) / "target").write_text("", encoding="utf-8")
            (Path(tmp) / "folder").mkdir()
            (directory / "github_codecov_token").symlink_to(Path(tmp) / "target")
            (directory / "notes").symlink_to(Path(tmp) / "folder")
            (directory / "github_codegen_app_client_id").symlink_to(Path(tmp) / "missing")
            created, present, strays, unclaimed, _ = self.run_scaffold(directory)
            self.assertEqual(created, [])
            self.assertEqual((strays, unclaimed), (["github_codecov_token"], ["notes"]))
            self.assertEqual(present, ["github_codegen_app_client_id"])

    def test_a_consumer_file_opening_with_a_short_name_is_owned_not_unclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            owned = ["examplerepo_service_key", "otherrepo_github_api.curlrc", "oldrepo_app_id"]
            for name in owned:
                (directory / name).write_text("", encoding="utf-8")
            created, present, strays, unclaimed, _ = self.run_scaffold(directory)
            self.assertEqual((strays, unclaimed), ([], []))
            self.assertFalse(set(owned) & set(present))
            self.assertFalse(set(owned) & set(created))

    def test_a_file_whose_first_field_names_no_short_name_is_unclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            names = [
                "fleet_service_key",
                "unknownrepo_service_key",
                "ExampleRepo_service_key",
                "examplerepo",
            ]
            for name in names:
                (directory / name).write_text("", encoding="utf-8")
            _, _, strays, unclaimed, _ = self.run_scaffold(directory)
            self.assertEqual(strays, [])
            self.assertEqual(unclaimed, sorted(names))

    def test_a_dry_run_creates_nothing_even_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".secrets"
            created, _, _, _, _ = self.run_scaffold(directory, dry_run=True)
            self.assertTrue(created)
            self.assertFalse(directory.exists())

    def test_an_unreadable_tree_fails_loud_naming_the_repo_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            with self.assertRaisesRegex(resolve_secrets.ResolveError, "owner/Other-Repo"):
                self.run_scaffold(directory, trees={"owner/ExampleRepo": TESTED})
            self.assertEqual(list(directory.iterdir()), [])

    def test_each_tree_is_read_on_that_entrys_ground_branch(self) -> None:
        fleet: dict[str, Any] = {
            "defaults": {"groundTruthBranch": "develop"},
            "repos": [{**FLEET["repos"][0], "groundTruthBranch": "trunk"}, FLEET["repos"][1]],
        }
        branches: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            resolve_secrets.scaffold(
                SECRETS, fleet, Path(tmp) / "s", True, tree_reader(TREES, branches)
            )
        self.assertEqual(
            branches, [("owner/ExampleRepo", "trunk"), ("owner/Other-Repo", "develop")]
        )

    def test_a_file_that_appears_after_the_listing_stops_the_run_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            real_open = os.open
            collided: list[str] = []

            def racing_open(path: Any, flags: int, mode: int = 0o777) -> int:
                if not collided:
                    collided.append(str(path))
                    Path(path).write_text("sentinel-raced-value-1\n", encoding="utf-8")
                return real_open(path, flags, mode)

            with (
                unittest.mock.patch.object(os, "open", racing_open),
                self.assertRaises(FileExistsError),
            ):
                self.run_scaffold(directory)
            self.assertEqual(
                Path(collided[0]).read_text(encoding="utf-8"), "sentinel-raced-value-1\n"
            )

    def test_a_repo_without_its_ground_branch_is_listed_and_the_others_are_still_scaffolded(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "s"
            created, _, strays, _, skipped = resolve_secrets.scaffold(
                SECRETS,
                FLEET,
                directory,
                False,
                tree_reader(TREES, missing=frozenset({"owner/Other-Repo"})),
            )
            self.assertEqual(strays, [])
            self.assertEqual(
                skipped, ["Other-Repo: the main branch of owner/Other-Repo does not exist yet"]
            )
            self.assertIn("github_examplerepo_codecov_token", created)
            self.assertTrue((directory / "github_examplerepo_codecov_token").exists())

    def scaffold_with_other_repo_skipped(
        self, directory: Path
    ) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
        return resolve_secrets.scaffold(
            SECRETS,
            FLEET,
            directory,
            False,
            tree_reader(TREES, missing=frozenset({"owner/Other-Repo"})),
        )

    def test_a_skipped_repos_existing_files_are_claimed_and_never_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            kept = directory / "github_other-repo_codecov_token"
            kept.write_text("sentinel-held-value-1\n", encoding="utf-8")
            created, present, strays, unclaimed, _ = self.scaffold_with_other_repo_skipped(
                directory
            )
            self.assertIn("github_other-repo_codecov_token", present)
            self.assertEqual((strays, unclaimed), ([], []))
            self.assertEqual(kept.read_text(encoding="utf-8"), "sentinel-held-value-1\n")
            self.assertFalse([c for c in created if "other-repo" in c])
            self.assertFalse(
                [p.name for p in directory.iterdir() if "other-repo" in p.name and p != kept]
            )

    def test_a_skipped_repos_wrong_scope_file_still_stops_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            (directory / "github_other-repo_codegen_app_client_id").write_text("", encoding="utf-8")
            created, _, strays, _, skipped = self.scaffold_with_other_repo_skipped(directory)
            self.assertEqual(strays, ["github_other-repo_codegen_app_client_id"])
            self.assertEqual(created, [])
            self.assertTrue(skipped)
            self.assertEqual(
                [p.name for p in directory.iterdir()], ["github_other-repo_codegen_app_client_id"]
            )

    def test_a_second_run_creates_nothing_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".secrets"
            first, _, _, _, _ = self.run_scaffold(directory)
            created, present, _, _, _ = self.run_scaffold(directory)
            self.assertEqual(created, [])
            self.assertEqual(present, first)


class ScaffoldCliCase(unittest.TestCase):
    """The command line around the scaffold: lists, exit status, and what it refuses to print."""

    def run_cli(
        self, *args: str, trees: Any = TREES, missing: frozenset[str] = frozenset()
    ) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "spec").mkdir()
            (Path(root) / "registry").mkdir()
            (Path(root) / "spec" / "secrets.json").write_text(json.dumps(SECRETS), encoding="utf-8")
            (Path(root) / "registry" / "repos.json").write_text(json.dumps(FLEET), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with (
                unittest.mock.patch.object(resolve_secrets, "ROOT", Path(root)),
                unittest.mock.patch.object(
                    resolve_secrets, "read_tree", tree_reader(trees, missing=missing)
                ),
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

    def test_a_repo_without_its_ground_branch_gets_its_own_heading_and_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".secrets"
            code, out, err = self.run_cli(
                "scaffold",
                "--secrets-dir",
                str(directory),
                missing=frozenset({"owner/Other-Repo"}),
            )
            self.assertEqual(code, 1)
            self.assertIn("Skipped, with no ground branch to read yet (1):", out)
            self.assertIn("Other-Repo: the main branch of owner/Other-Repo does not exist yet", out)
            self.assertIn("Created (", out)
            self.assertTrue((directory / "github_examplerepo_codecov_token").exists())
            self.assertIn("once each branch exists", err)

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


class CompareStoresCase(unittest.TestCase):
    """The one comparison of live store names with what a repo owes, whatever reads the stores."""

    REQUIRED: ClassVar[dict[str, set[str]]] = {"actions": {"A", "B"}, "dependabot": {"A"}}

    def test_each_level_is_reported_in_the_audit_order(self) -> None:
        live = {"actions": {"A", "FORBID", "EXTRA"}, "dependabot": set()}
        findings = secret_names.compare_stores(self.REQUIRED, {"FORBID"}, live)
        self.assertEqual(
            findings,
            [
                ("OK", "secrets: A is in the actions store"),
                ("DEFECT", "secrets: B missing from the actions store"),
                ("DEFECT", "secrets: A missing from the dependabot store"),
                ("DEFECT", "secrets: forbidden FORBID present in the actions store"),
                (
                    "DRIFT",
                    "secrets: EXTRA in the actions store is claimed in that store by no applicable mechanism (stale?)",
                ),
                (
                    "DRIFT",
                    "secrets: FORBID in the actions store is claimed in that store by no applicable mechanism (stale?)",
                ),
            ],
        )

    def test_a_clean_set_has_only_ok_findings(self) -> None:
        live = {"actions": {"A", "B"}, "dependabot": {"A"}}
        levels = {level for level, _ in secret_names.compare_stores(self.REQUIRED, set(), live)}
        self.assertEqual(levels, {"OK"})

    def test_a_name_only_the_other_store_owes_is_unclaimed_where_it_is_not_owed(self) -> None:
        live = {"actions": {"A", "B"}, "dependabot": {"A", "B"}}
        findings = secret_names.compare_stores(self.REQUIRED, set(), live)
        self.assertEqual(
            [f for f in findings if f[0] != "OK"],
            [
                (
                    "DRIFT",
                    "secrets: B in the dependabot store is claimed in that store by no applicable mechanism (stale?)",
                )
            ],
        )


class TreeBlobsCase(unittest.TestCase):
    """The one parse of a git trees response, shared by the audit and the provisioning."""

    GOOD: ClassVar[dict[str, Any]] = {
        "tree": [
            {"type": "blob", "path": "a.py", "sha": "s1", "mode": "100644"},
            {"type": "tree", "path": "src"},
        ],
        "truncated": False,
    }

    def test_blobs_map_to_their_sha_and_mode_and_trees_are_dropped(self) -> None:
        self.assertEqual(secret_names.tree_blobs(self.GOOD), {"a.py": ("s1", "100644")})

    def test_each_unreadable_response_is_none(self) -> None:
        for label, response in (
            ("none", None),
            ("empty", {}),
            ("no tree key", {"truncated": False}),
            ("truncated", {**self.GOOD, "truncated": True}),
            ("a non-list tree", {"tree": "x"}),
            ("a list response", []),
        ):
            with self.subTest(label):
                self.assertIsNone(secret_names.tree_blobs(response))

    def test_the_audit_reads_through_it_and_keeps_none_for_an_unreadable_tree(self) -> None:
        head = {"commit": {"commit": {"tree": {"sha": "abc"}}}}
        with unittest.mock.patch.object(audit, "gh", return_value=self.GOOD):
            self.assertEqual(audit.repo_tree_blobs("owner/R", head), {"a.py": ("s1", "100644")})
        with unittest.mock.patch.object(audit, "gh", return_value={"truncated": True}):
            self.assertIsNone(audit.repo_tree_blobs("owner/R", head))
        self.assertIsNone(audit.repo_tree_blobs("owner/R", None))


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

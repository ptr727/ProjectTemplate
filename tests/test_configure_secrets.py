#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's secrets group by running the script's own lines.

The shell is lifted out of the file rather than restated here, and run under the script's own `set` options.
The secrets directory is synthetic and holds invented sentinel values.
A stub `gh` goes first on PATH, logs its arguments, and saves each `secret set` call's stdin.
No call reaches GitHub, and no real secrets directory is read.
Every case that sets a secret asserts the sentinel reached the stub's stdin and never an argument, stdout, or stderr.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from host_capability import requires_symlink
from test_configure_environments import JQR, REPORTERS, lift, require

HUB = Path(__file__).resolve().parents[1]
SPEC = HUB / "spec"
LIBRARY = HUB / "lib" / "sh" / "secrets.sh"
sys.path.insert(0, str(SPEC))
import resolve_secrets

SECRETS = json.loads((SPEC / "secrets.json").read_text(encoding="utf-8"))

# The library reads file modes through `stat -c` and is tested only on Linux, so a case that needs a mode is too.
POSIX_ONLY = unittest.skipUnless(
    sys.platform == "linux",
    "the secrets library reads POSIX modes, as tests/test_secrets_sh.py does",
)
# A stub gh is a shebang script on PATH, which Windows' CreateProcess cannot run when python spawns gh.
# Only a `gh.exe` is found there, so a case that has python spawn the stub is skipped on Windows.
# The same behavior runs in process, with run_subprocess stubbed, in tests/test_resolve_secrets.py.
NEEDS_SPAWNED_GH = unittest.skipIf(
    os.name == "nt", "CreateProcess runs only a gh.exe, never a shebang stub on PATH"
)

REGISTRY = {
    "repos": [
        {
            "name": "ExampleRepo",
            "url": "https://github.com/owner/ExampleRepo",
            "status": "cataloged",
            "types": ["python"],
            "publish": [{"target": "docker", "mechanism": "static-secret"}],
        }
    ]
}

PLAN = (
    r"(plan_secrets\(\) \{.*?^\})",
    r"(report_secret_problems\(\) \{.*?^\})",
    r"(dry_run_secrets\(\) \{.*?^\})",
    r"(apply_secrets\(\) \{.*?^\})",
)

GH_STUB = """#!/bin/sh
printf '%s\\n' "$*" >>"$GH_DIR/argv"
case "$*" in
"api repos/owner/ExampleRepo/git/trees/"*"?recursive=1")
    if [ -f "$GH_DIR/tree.status" ]; then
        echo "gh: error (HTTP $(cat "$GH_DIR/tree.status"))" >&2
        exit 1
    fi
    cat "$GH_DIR/tree.json"
    ;;
"api --paginate repos/owner/ExampleRepo/actions/secrets --jq .secrets[].name") cat "$GH_DIR/list.actions" ;;
"api --paginate repos/owner/ExampleRepo/dependabot/secrets --jq .secrets[].name") cat "$GH_DIR/list.dependabot" ;;
"secret set "*)
    store=""
    prev=""
    for arg in "$@"; do
        if [ "$prev" = --app ]; then store="$arg"; fi
        prev="$arg"
    done
    cat >"$GH_DIR/stdin.$3.$store"
    if [ "$3" = "${GH_FAIL_NAME:-}" ]; then
        echo "stub failure" >&2
        exit 1
    fi
    ;;
*)
    echo "unexpected gh call: $*" >&2
    exit 97
    ;;
esac
"""


def sentinel(name: str) -> str:
    return f"sentinel-{name.lower().replace('_', '-')}-value-1"


class Fixture:
    """A scratch tree: a secrets directory, a stub gh, and the resolver's plan for ExampleRepo."""

    def __init__(self, tmp: str, tree: tuple[str, ...] = ("tests/test_a.py",)) -> None:
        self.root = Path(tmp)
        self.secrets = self.root / "secrets"
        self.secrets.mkdir(mode=0o700)
        self.gh_dir = self.root / "gh"
        self.gh_dir.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "gh"
        stub.write_text(GH_STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.registry = self.root / "repos.json"
        self.registry.write_text(json.dumps(REGISTRY), encoding="utf-8")
        self.live: dict[str, set[str]] | None = None
        self.write_tree(tree)
        for store in ("actions", "dependabot"):
            (self.gh_dir / f"list.{store}").write_text("", encoding="utf-8")

    def write_tree(self, tree: tuple[str, ...], truncated: bool = False) -> None:
        self.tree = tree
        payload = {"tree": [{"type": "blob", "path": p} for p in tree], "truncated": truncated}
        (self.gh_dir / "tree.json").write_text(json.dumps(payload), encoding="utf-8")

    def names_json(self) -> str:
        """The resolver's own plan for ExampleRepo, so the harness feeds the script what it really gets.

        It is computed in process, with the tree and the live stores handed over, so no gh is spawned.
        """
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        live = self.live
        plan = resolve_secrets.resolve_names(
            SECRETS,
            registry,
            "ExampleRepo",
            "owner/ExampleRepo",
            lambda slug, branch: set(self.tree),
            (lambda slug: live) if live is not None else None,
        )
        return json.dumps(plan)

    def fill(self, file_name: str, value: str, mode: int = 0o600) -> Path:
        path = self.secrets / file_name
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(mode)
        return path

    def fill_all(self, skip: tuple[str, ...] = ()) -> None:
        """Every file ExampleRepo owes, each holding its sentinel."""
        for row in json.loads(self.names_json())["rows"]:
            if row["file"] not in skip:
                self.fill(row["file"], sentinel(row["name"]))

    def run(
        self, body: str, env_extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        bash = require("bash", "jq", "sed", "grep")
        options = lift(r"^(set -[A-Za-z]+ [a-z]+)$")
        lifted = "\n".join(lift(pattern) for pattern in PLAN)
        names = self.names_json()
        # Resolving the names read the tree through the stub, which is not a call the script under test made.
        (self.gh_dir / "argv").unlink(missing_ok=True)
        script = "\n".join(
            [
                options,
                lift(JQR),
                lift(REPORTERS),
                f". {shlex.quote(str(LIBRARY))}",
                lifted,
                "repo=owner/ExampleRepo",
                f"secret_names_json={shlex.quote(names)}",
                f"dir={shlex.quote(str(self.secrets))}",
                body,
            ]
        )
        env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        env["GH_DIR"] = str(self.gh_dir)
        env.update(env_extra or {})
        return subprocess.run(
            [bash, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
            env=env,
        )

    def argv(self) -> str:
        path = self.gh_dir / "argv"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def written(self) -> dict[str, str]:
        """Each saved stdin keyed by `NAME.store`."""
        return {
            p.name.removeprefix("stdin."): p.read_text(encoding="utf-8")
            for p in sorted(self.gh_dir.glob("stdin.*"))
        }


def assert_no_value_leaked(case: unittest.TestCase, fix: Fixture, *outputs: str) -> None:
    """No sentinel appears in the logged arguments or in any captured stream."""
    haystack = fix.argv() + "".join(outputs)
    case.assertNotIn("sentinel-", haystack)


APPLY = 'plan_secrets "$dir"\napply_secrets "$dir"\n'


@POSIX_ONLY
class ApplyCase(unittest.TestCase):
    """The apply path: values go to stdin only, in every store a name needs."""

    def test_every_name_reaches_stdin_in_each_of_its_stores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all()
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 0, result.stderr)
            written = fix.written()
            # The baseline, docker, and codecov names each need both stores.
            self.assertEqual(len(written), 10, sorted(written))
            for name in (
                "CODEGEN_APP_CLIENT_ID",
                "CODEGEN_APP_PRIVATE_KEY",
                "DOCKER_HUB_USERNAME",
                "DOCKER_HUB_ACCESS_TOKEN",
                "CODECOV_TOKEN",
            ):
                for store in ("actions", "dependabot"):
                    with self.subTest(name=name, store=store):
                        self.assertEqual(written[f"{name}.{store}"], sentinel(name) + "\n")
            assert_no_value_leaked(self, fix, result.stdout, result.stderr)

    def test_a_fleet_name_is_read_from_the_fleet_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all()
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Set DOCKER_HUB_USERNAME (fleet) in the actions store", result.stdout)

    def test_a_repository_name_is_read_from_the_repository_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all()
            self.assertTrue((fix.secrets / "github_examplerepo_codecov_token").exists())
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                fix.written()["CODECOV_TOKEN.actions"], sentinel("CODECOV_TOKEN") + "\n"
            )
            self.assertIn("Set CODECOV_TOKEN (repository)", result.stdout)

    def test_a_python_repo_without_tests_owes_no_codecov_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp, tree=("src/pkg/mod.py",))
            fix.fill_all()
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse([k for k in fix.written() if k.startswith("CODECOV_TOKEN")])

    def test_a_missing_file_is_listed_while_the_others_are_still_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all(skip=("github_docker_hub_access_token",))
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Secrets with no file, so not set:", result.stdout)
            self.assertIn(
                "DOCKER_HUB_ACCESS_TOKEN (fleet, stores: actions dependabot)", result.stdout
            )
            self.assertIn(str(fix.secrets / "github_docker_hub_access_token"), result.stdout)
            self.assertNotIn("whose file was refused", result.stdout)
            written = fix.written()
            self.assertIn("CODECOV_TOKEN.actions", written)
            self.assertNotIn("DOCKER_HUB_ACCESS_TOKEN.actions", written)
            assert_no_value_leaked(self, fix, result.stdout, result.stderr)

    def test_unsafe_files_are_refusals_kept_apart_from_the_missing_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all(
                skip=(
                    "github_docker_hub_username",
                    "github_docker_hub_access_token",
                    "github_examplerepo_codecov_token",
                )
            )
            fix.fill("github_docker_hub_username", sentinel("DOCKER_HUB_USERNAME"), mode=0o644)
            (fix.secrets / "github_examplerepo_codecov_token").write_text("", encoding="utf-8")
            (fix.secrets / "github_examplerepo_codecov_token").chmod(0o600)
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Secrets whose file was refused, so not set:", result.stdout)
            self.assertIn("DOCKER_HUB_USERNAME: ", result.stdout)
            self.assertIn("is mode 644", result.stdout)
            self.assertIn("CODECOV_TOKEN: ", result.stdout)
            self.assertIn("is empty, an unfilled placeholder", result.stdout)
            # The one name with no file at all is the only entry of the missing list.
            missing = result.stdout.split("Secrets with no file, so not set:")[1].split(
                "Secrets whose file"
            )[0]
            self.assertIn("DOCKER_HUB_ACCESS_TOKEN", missing)
            self.assertNotIn("DOCKER_HUB_USERNAME", missing)
            self.assertNotIn("CODECOV_TOKEN", missing)
            written = fix.written()
            self.assertFalse([k for k in written if k.startswith("DOCKER_HUB_USERNAME")])
            self.assertFalse([k for k in written if k.startswith("CODECOV_TOKEN")])
            assert_no_value_leaked(self, fix, result.stdout, result.stderr)

    def test_a_whitespace_only_file_is_refused_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all(skip=("github_docker_hub_username",))
            fix.fill("github_docker_hub_username", "  \t ")
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 1)
            self.assertIn("is empty, an unfilled placeholder", result.stdout)

    @requires_symlink
    def test_a_dangling_link_is_a_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all(skip=("github_docker_hub_username",))
            (fix.secrets / "github_docker_hub_username").symlink_to(fix.root / "nowhere")
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Secrets whose file was refused, so not set:", result.stdout)
            self.assertIn("DOCKER_HUB_USERNAME: ", result.stdout)
            self.assertIn("symlink that does not resolve", result.stdout)
            self.assertNotIn("Secrets with no file", result.stdout)

    def test_a_refused_repository_file_does_not_fall_back_to_a_fleet_file(self) -> None:
        """The scopes are strict, so a refused file leaves the name unset rather than reading another file."""
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all(skip=("github_examplerepo_codecov_token",))
            fix.fill("github_examplerepo_codecov_token", sentinel("CODECOV_TOKEN"), mode=0o644)
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 1)
            self.assertIn("CODECOV_TOKEN: ", result.stdout)
            self.assertFalse([k for k in fix.written() if k.startswith("CODECOV_TOKEN")])

    def test_a_failed_write_stops_the_run_with_nothing_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all()
            result = fix.run(APPLY, {"GH_FAIL_NAME": "CODEGEN_APP_PRIVATE_KEY"})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stub failure", result.stderr)
            self.assertNotIn("Configuration applied", result.stdout + result.stderr)
            written = fix.written()
            self.assertIn("CODEGEN_APP_PRIVATE_KEY.actions", written)
            # Rows run in name order, so nothing after the failing name was attempted.
            self.assertNotIn("CODEGEN_APP_PRIVATE_KEY.dependabot", written)
            self.assertFalse([k for k in written if k.startswith("DOCKER_HUB")])
            assert_no_value_leaked(self, fix, result.stdout, result.stderr)


@POSIX_ONLY
class StrayCase(unittest.TestCase):
    """A file at the wrong scope aborts before any write, and is never read."""

    def test_a_fleet_twin_of_a_repository_name_aborts_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all()
            fix.fill("github_codecov_token", "sentinel-stray-fleet-value-1")
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 1)
            self.assertIn(str(fix.secrets / "github_codecov_token"), result.stderr)
            self.assertIn("Aborting before any write", result.stderr)
            self.assertEqual(fix.argv(), "")
            self.assertNotIn("sentinel-", result.stdout + result.stderr)

    def test_a_repository_twin_of_a_fleet_name_aborts_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all()
            fix.fill("github_examplerepo_docker_hub_username", "sentinel-stray-repo-value-1")
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 1)
            self.assertIn(
                str(fix.secrets / "github_examplerepo_docker_hub_username"), result.stderr
            )
            self.assertEqual(fix.argv(), "")

    def test_every_stray_is_reported_and_the_dry_run_aborts_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all()
            fix.fill("github_codecov_token", "sentinel-stray-fleet-value-1")
            fix.fill("github_examplerepo_nuget_username", "sentinel-unclaimed-value-1")
            fix.fill("github_examplerepo_codegen_app_client_id", "sentinel-stray-repo-value-1")
            result = fix.run('plan_secrets "$dir"\ndry_run_secrets "$dir"\n')
            self.assertEqual(result.returncode, 1)
            self.assertIn("github_codecov_token", result.stderr)
            self.assertIn("github_examplerepo_codegen_app_client_id", result.stderr)
            # A name this repo does not owe has no twin to collide with, so it is not a stray.
            self.assertNotIn("github_examplerepo_nuget_username", result.stderr)
            self.assertEqual(fix.argv(), "")
            self.assertNotIn("Dry run", result.stdout)

    def test_a_stray_is_never_opened(self) -> None:
        """A stray with no read permission is still named and not refused for it, since nothing opens it."""
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all()
            fix.fill("github_codecov_token", "sentinel-stray-fleet-value-1", mode=0o000)
            result = fix.run(APPLY)
            self.assertEqual(result.returncode, 1)
            self.assertIn("github_codecov_token", result.stderr)
            self.assertNotIn("is mode", result.stderr)


@POSIX_ONLY
class DryRunCase(unittest.TestCase):
    """The dry run lists by store and scope, and makes no write."""

    def test_it_lists_each_store_and_calls_no_gh_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all()
            result = fix.run('plan_secrets "$dir"\ndry_run_secrets "$dir"\n')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Secrets that would be set in the actions store", result.stdout)
            self.assertIn("Secrets that would be set in the dependabot store", result.stdout)
            self.assertIn(
                "CODECOV_TOKEN (repository) from github_examplerepo_codecov_token", result.stdout
            )
            self.assertNotIn("NUGET_USERNAME", result.stdout)
            self.assertIn(
                "DOCKER_HUB_USERNAME (fleet) from github_docker_hub_username", result.stdout
            )
            self.assertIn("Only the secrets group has a dry run", result.stdout)
            self.assertEqual(fix.argv(), "")
            self.assertEqual(fix.written(), {})
            assert_no_value_leaked(self, fix, result.stdout, result.stderr)

    def test_it_exits_non_zero_when_a_real_apply_would(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.fill_all(skip=("github_docker_hub_username",))
            result = fix.run('plan_secrets "$dir"\ndry_run_secrets "$dir"\n')
            self.assertEqual(result.returncode, 1)
            self.assertIn("Secrets with no file, so not set:", result.stdout)
            self.assertEqual(fix.argv(), "")


@NEEDS_SPAWNED_GH
class LoadNamesCase(unittest.TestCase):
    """The tree read that feeds the resolver, through the script's own function."""

    def load(self, fix: Fixture) -> subprocess.CompletedProcess[str]:
        bash = require("bash", "jq", "sed", "grep")
        options = lift(r"^(set -[A-Za-z]+ [a-z]+)$")
        script = "\n".join(
            [
                options,
                lift(JQR),
                lift(r"(load_secret_names\(\) \{.*?^\})"),
                f"py_cmd=({shlex.quote(sys.executable)})",
                f"resolve_secrets={shlex.quote(str(SPEC / 'resolve_secrets.py'))}",
                f"secrets_spec={shlex.quote(str(SPEC / 'secrets.json'))}",
                f"registry={shlex.quote(str(fix.registry))}",
                "repo=owner/ExampleRepo",
                "name=ExampleRepo",
                "if load_secret_names; then",
                '  printf "%s\\n" "$secret_names_json"',
                "else",
                '  echo "LOAD_FAILED"',
                "fi",
            ]
        )
        env = dict(os.environ, PATH=f"{fix.bin}{os.pathsep}{os.environ['PATH']}")
        env["GH_DIR"] = str(fix.gh_dir)
        return subprocess.run(
            [bash, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
            env=env,
        )

    def test_the_resolver_reads_the_tree_that_decides_codecov(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp, tree=("tests/test_a.py",))
            result = self.load(fix)
            self.assertEqual(result.returncode, 0, result.stderr)
            names = [r["name"] for r in json.loads(result.stdout)["rows"]]
            self.assertIn("CODECOV_TOKEN", names)
            fix.write_tree(("src/mod.py",))
            names = [r["name"] for r in json.loads(self.load(fix).stdout)["rows"]]
            self.assertNotIn("CODECOV_TOKEN", names)

    def test_a_truncated_tree_fails_rather_than_dropping_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            fix.write_tree(("tests/test_a.py",), truncated=True)
            result = self.load(fix)
            self.assertIn("LOAD_FAILED", result.stdout)
            self.assertIn("unreadable or truncated", result.stderr)

    def test_the_tree_is_read_on_the_registry_ground_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            self.assertIn("git/trees/main?recursive=1", self.load_and_log(fix))
            registry: dict[str, object] = {
                "repos": [{**REGISTRY["repos"][0], "groundTruthBranch": "trunk"}]
            }
            fix.registry.write_text(json.dumps(registry), encoding="utf-8")
            self.assertIn("git/trees/trunk?recursive=1", self.load_and_log(fix))
            registry = {"repos": REGISTRY["repos"], "defaults": {"groundTruthBranch": "develop"}}
            fix.registry.write_text(json.dumps(registry), encoding="utf-8")
            self.assertIn("git/trees/develop?recursive=1", self.load_and_log(fix))

    def test_a_missing_ground_branch_loads_as_a_skip_and_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            (fix.gh_dir / "tree.status").write_text("404", encoding="utf-8")
            result = self.load(fix)
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["rows"], [])
            self.assertEqual(
                plan["skip"], "the main branch of owner/ExampleRepo does not exist yet"
            )
            (fix.gh_dir / "tree.status").write_text("500", encoding="utf-8")
            self.assertIn("LOAD_FAILED", self.load(fix).stdout)

    def load_and_log(self, fix: Fixture) -> str:
        (fix.gh_dir / "argv").unlink(missing_ok=True)
        result = self.load(fix)
        self.assertEqual(result.returncode, 0, result.stderr)
        return fix.argv()


class CheckCase(unittest.TestCase):
    """The check group compares live names with what the repo owes, in both stores."""

    def check(self, fix: Fixture) -> subprocess.CompletedProcess[str]:
        lifted = lift(r"(check_secrets\(\) \{.*?^\})")
        # The tree and store reads have their own cases, so the plan the fixture holds stands in for them here.
        return fix.run(
            f"{lifted}\nload_secret_names() {{ secret_names_skip=; }}\nsecrets_registered=true\nname=ExampleRepo\n"
            'check_secrets\necho "FAILED=$FAILED"\n'
        )

    def list_stores(self, fix: Fixture, actions: list[str], dependabot: list[str]) -> None:
        fix.live = {"actions": set(actions), "dependabot": set(dependabot)}

    OWED: ClassVar[list[str]] = [
        "CODECOV_TOKEN",
        "CODEGEN_APP_CLIENT_ID",
        "CODEGEN_APP_PRIVATE_KEY",
        "DOCKER_HUB_ACCESS_TOKEN",
        "DOCKER_HUB_USERNAME",
    ]

    def test_every_owed_name_present_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            self.list_stores(fix, self.OWED, self.OWED)
            result = self.check(fix)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("  FAIL ", result.stdout)
            self.assertIn("FAILED=0", result.stdout)
            self.assertIn("never readable via the API", result.stdout)

    def test_an_absent_name_fails_in_the_store_it_is_missing_from(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            self.list_stores(fix, self.OWED, [n for n in self.OWED if n != "CODECOV_TOKEN"])
            result = self.check(fix)
            self.assertIn(
                "  FAIL secrets: CODECOV_TOKEN missing from the dependabot store", result.stdout
            )
            self.assertNotIn("missing from the actions store", result.stdout)
            self.assertIn("FAILED=1", result.stdout)

    def test_a_forbidden_name_present_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            self.list_stores(fix, [*self.OWED, "CODEGEN_APP_ID"], self.OWED)
            result = self.check(fix)
            self.assertIn(
                "  FAIL secrets: forbidden CODEGEN_APP_ID present in the actions store",
                result.stdout,
            )
            self.assertIn("FAILED=1", result.stdout)

    def test_an_unclaimed_name_is_reported_and_never_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            self.list_stores(fix, [*self.OWED, "LEFTOVER_NAME"], self.OWED)
            result = self.check(fix)
            self.assertIn(
                "secrets: LEFTOVER_NAME in the actions store is claimed by no applicable mechanism",
                result.stdout,
            )
            self.assertIn("FAILED=0", result.stdout)
            # Reporting never deletes, so no write call was made.
            self.assertNotIn("secret delete", fix.argv())


class ScriptWiringCase(unittest.TestCase):
    """The group is wired into the script's own flow in the order the header describes."""

    def test_apply_runs_the_secrets_group_after_the_agent_policy_files(self) -> None:
        text = (HUB / "repo-config" / "configure.sh").read_text(encoding="utf-8")
        apply_body = text.split("cmd_apply() {\n", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(apply_body.index("Agent policy files"), apply_body.index("apply_secrets "))
        # The plan, and with it any stray abort, runs before the first write.
        self.assertLess(
            apply_body.index("plan_secrets "), apply_body.index("General repository settings")
        )
        self.assertLess(
            apply_body.index("dry_run_secrets "), apply_body.index("General repository settings")
        )


class PrologueCase(unittest.TestCase):
    """The real script, copied whole, gets past its prologue to its first API call."""

    def run_real(self, command: str) -> tuple[subprocess.CompletedProcess[str], str]:
        bash = require("bash", "jq", "sed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "repo-config").mkdir()
            shutil.copy(HUB / "repo-config" / "configure.sh", root / "repo-config" / "configure.sh")
            for rel in (
                "scripts/agent_policy.py",
                "scripts/gh_graphql.py",
                "scripts/pr_review_policy.py",
                "spec/agent-policy.default.json",
                "spec/resolve_description.py",
                "spec/resolve_secrets.py",
                "spec/secret_names.py",
                "spec/validate.py",
                "spec/secrets.json",
                "repo-config/main.json",
                "repo-config/develop.json",
                "repo-config/operational/develop.json",
                "repo-config/settings.json",
                "repo-config/labels.json",
                "repo-config/project.json",
                "lib/sh/secrets.sh",
            ):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(HUB / rel, root / rel)
            (root / "registry").mkdir()
            (root / "registry" / "repos.json").write_text(
                json.dumps(
                    {
                        "repos": [
                            {
                                "name": "ExampleRepo",
                                "url": "https://github.com/owner/ExampleRepo",
                                "status": "cataloged",
                                "types": ["source-only"],
                                "workflowModel": "release",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            calls = root / "gh-calls"
            stub = root / "bin" / "gh"
            stub.write_text(f'#!/bin/sh\necho "$*" >>"{calls}"\nexit 1\n', encoding="utf-8")
            stub.chmod(0o755)
            env = dict(os.environ, PATH=f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}")
            result = subprocess.run(
                [bash, str(root / "repo-config" / "configure.sh"), command, "owner/ExampleRepo"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
                env=env,
            )
            return result, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def assert_past_the_prologue(self, command: str) -> None:
        result, calls = self.run_real(command)
        self.assertNotIn("command not found", result.stderr)
        self.assertNotIn("to resolve the entry", result.stderr)
        self.assertNotIn("Could not compose", result.stderr)
        self.assertNotEqual(calls, "", result.stderr)

    def test_apply_passes_the_prologue_to_the_first_api_call(self) -> None:
        self.assert_past_the_prologue("apply")

    @NEEDS_SPAWNED_GH
    def test_check_passes_the_prologue_and_runs_its_secrets_group(self) -> None:
        """Check runs on to its secrets group, where Python spawns gh, so the stub must be runnable by CreateProcess.

        The prologue is shared with apply, which keeps that coverage on every platform.
        """
        self.assert_past_the_prologue("check")


class StatusGateCase(unittest.TestCase):
    """Only a cataloged registry entry owes secrets, as only those are audited."""

    GATE = r'(secrets_registered=false\nsecret_names_skip=""\nsecrets_skip_reason=.*?\nfi\n)'

    def gate(self, status: str) -> subprocess.CompletedProcess[str]:
        bash = require("bash")
        options = lift(r"^(set -[A-Za-z]+ [a-z]+)$")
        script = "\n".join(
            [
                options,
                "repo=owner/ExampleRepo\nregistry=registry/repos.json",
                f"archived_status={shlex.quote(status)}",
                lift(self.GATE),
                'echo "registered=$secrets_registered"',
                'echo "reason=$secrets_skip_reason"',
            ]
        )
        return subprocess.run(
            [bash, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    def test_a_cataloged_entry_is_registered(self) -> None:
        result = self.gate("cataloged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("registered=true", result.stdout)

    def test_an_excluded_entry_is_skipped_with_its_status_named(self) -> None:
        result = self.gate("excluded")
        self.assertIn("registered=false", result.stdout)
        self.assertIn("the registry status 'excluded'", result.stdout)

    def test_no_entry_is_skipped_and_says_so(self) -> None:
        result = self.gate("")
        self.assertIn("registered=false", result.stdout)
        self.assertIn("has no entry in registry/repos.json", result.stdout)

    def test_check_asserts_nothing_for_a_skipped_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            lifted = lift(r"(check_secrets\(\) \{.*?^\})")
            result = fix.run(
                f"{lifted}\nsecrets_registered=false\n"
                "secrets_skip_reason=\"owner/ExampleRepo has the registry status 'excluded'\"\n"
                'check_secrets\necho "FAILED=$FAILED"\n'
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("status 'excluded'", result.stdout)
            self.assertIn("FAILED=0", result.stdout)
            self.assertEqual(fix.argv(), "")

    def test_the_apply_group_is_skipped_with_a_note_and_no_write(self) -> None:
        text = (HUB / "repo-config" / "configure.sh").read_text(encoding="utf-8")
        apply_body = text.split("cmd_apply() {\n", 1)[1].split("\n}\n", 1)[0]
        self.assertIn(
            'if [ "$secrets_registered" = true ]; then\n        apply_secrets', apply_body
        )
        self.assertIn("provisioned only for a cataloged registry entry", apply_body)


class MissingBranchCase(unittest.TestCase):
    """A ground branch that does not exist yet skips the secrets group without aborting the other groups."""

    def apply_body(self) -> str:
        text = (HUB / "repo-config" / "configure.sh").read_text(encoding="utf-8")
        return text.split("cmd_apply() {\n", 1)[1].split("\n}\n", 1)[0]

    def test_check_reports_the_reason_as_a_note_and_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            lifted = lift(r"(check_secrets\(\) \{.*?^\})")
            result = fix.run(
                f"{lifted}\nload_secret_names() {{ secret_names_skip='the main branch of owner/ExampleRepo does not exist yet'; }}\n"
                'secrets_registered=true\ncheck_secrets\necho "FAILED=$FAILED"\n'
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("the main branch of owner/ExampleRepo does not exist yet", result.stdout)
            self.assertNotIn("  FAIL ", result.stdout)
            self.assertIn("FAILED=0", result.stdout)

    def test_dry_run_prints_the_reason_and_exits_non_zero(self) -> None:
        body = self.apply_body()
        start = body.index('if [ -n "$secret_names_skip" ]; then')
        block = body[start : start + 450]
        self.assertIn("Dry run for $repo. Secrets would not be set", block)
        self.assertIn("exit 1", block)
        self.assertLess(start, body.index("General repository settings"))

    def test_apply_notes_the_reason_sets_nothing_and_ends_non_zero(self) -> None:
        body = self.apply_body()
        self.assertIn(
            'if [ "$secrets_registered" = true ] && [ -n "$secret_names_skip" ]; then\n'
            '        echo "Note: no secret was set',
            body,
        )
        # The skip branch precedes the write, and the final exit follows every group.
        self.assertLess(body.index("no secret was set"), body.index("apply_secrets "))
        tail = body[body.index("no secret was set") :]
        self.assertLess(tail.index("exit 1"), tail.index("Configuration applied to $repo. Run"))
        # The plan, which reads the secrets directory, is not run for a skipped repo.
        self.assertIn("else\n            plan_secrets", body)


class SlugRefusalCase(unittest.TestCase):
    """A slug the registry entry does not name stops the run before any write, and fails the check."""

    @NEEDS_SPAWNED_GH
    def test_the_load_fails_for_a_slug_the_entry_does_not_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            other = {
                "repos": [
                    {**REGISTRY["repos"][0], "url": "https://github.com/otherowner/ExampleRepo"}
                ]
            }
            fix.registry.write_text(json.dumps(other), encoding="utf-8")
            result = LoadNamesCase().load(fix)
            self.assertIn("LOAD_FAILED", result.stdout)
            self.assertIn("Refusing", result.stderr)
            self.assertIn("otherowner/ExampleRepo", result.stderr)
            self.assertEqual(fix.argv(), "")

    def test_check_fails_when_the_names_cannot_be_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = Fixture(tmp)
            lifted = lift(r"(check_secrets\(\) \{.*?^\})")
            result = fix.run(
                f"{lifted}\nload_secret_names() {{ return 1; }}\nsecrets_registered=true\n"
                'check_secrets\necho "FAILED=$FAILED"\n'
            )
            self.assertIn("  FAIL secrets - could not resolve", result.stdout)
            self.assertIn("FAILED=1", result.stdout)

    def test_apply_stops_before_any_write_when_the_names_cannot_be_resolved(self) -> None:
        text = (HUB / "repo-config" / "configure.sh").read_text(encoding="utf-8")
        apply_body = text.split("cmd_apply() {\n", 1)[1].split("\n}\n", 1)[0]
        guard = apply_body.index("if ! load_secret_names; then")
        self.assertIn("exit 1", apply_body[guard : guard + 200])
        self.assertLess(guard, apply_body.index("General repository settings"))


if __name__ == "__main__":
    unittest.main()

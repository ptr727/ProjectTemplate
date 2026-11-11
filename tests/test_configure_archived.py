#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's registry region by running its own lines.

The shell is lifted out of the file rather than restated here.
An edit that removes the behavior therefore fails these tests instead of leaving a reimplementation to agree with itself. The
region under test makes no `gh` call of its own, and the harness assigns every variable the region reads.
So the cases running the lifted region need no `gh` stub.
Under check, a cataloged, unlisted, or absent entry falls through to a marker printed just after that region.
Under apply, only a listed one does, and an unlisted entry or an absent registry fails the run.
An archived entry exits before reaching the marker.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from host_capability import bash_or_skip

CONFIGURE = Path(__file__).resolve().parents[1] / "repo-config" / "configure.sh"


def lift(pattern: str) -> str:
    """The one region of configure.sh matching `pattern`, or a failure naming what was missing."""
    text = CONFIGURE.read_text(encoding="utf-8")
    found = re.findall(pattern, text, re.MULTILINE | re.DOTALL)
    if len(found) != 1:
        raise AssertionError(
            f"expected one match in {CONFIGURE.name} for {pattern!r}, found {len(found)}"
        )
    return found[0]


def require(*tools: str) -> str:
    """The bash path, skipping instead of failing where a tool the harness shells out to is absent."""
    for tool in tools:
        if shutil.which(tool) is None:
            raise unittest.SkipTest(f"no {tool} on PATH, so the script's own lines cannot be run")
    return bash_or_skip()


def run_bash(script: str, *tools: str) -> subprocess.CompletedProcess[str]:
    """The script under the same shell options configure.sh sets, with a bounded wait.

    The options are lifted rather than typed.
    That is because a harness running without them is blind to exactly the error handling the lines under test rely on.
    """
    bash = require("bash", *tools)
    options = lift(r"^(set -[A-Za-z]+ [a-z]+)$")
    return subprocess.run(
        [bash, "-c", f"{options}\n{script}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


REGISTRY_REGION = lift(r"^(jqr\(\) \{.*?\}\n)") + lift(
    r'(if \[ -f "\$registry" \]; then\n    if ! archived_status=.*?\n^fi\n)'
)


class RegistryRegionCase(unittest.TestCase):
    """The helpers that run the lifted registry region against a constructed registry."""

    def registry(self, tmp: str, entries: list[dict[str, object]]) -> Path:
        path = Path(tmp) / "repos.json"
        path.write_text(json.dumps({"repos": entries}), encoding="utf-8")
        return path

    def harness(self, registry: Path, name: str, cmd: str) -> subprocess.CompletedProcess[str]:
        script = (
            f"registry={shlex.quote(str(registry))}\nname={shlex.quote(name)}\n"
            f"cmd={shlex.quote(cmd)}\nrepo={shlex.quote('owner/' + name)}\n"
            f"{REGISTRY_REGION}echo REACHED_NEXT\n"
        )
        return run_bash(script, "jq")


class ArchivedRepositoryCase(RegistryRegionCase):
    """A repository the registry marks archived is out of scope, for apply and check alike."""

    def test_an_archived_entry_exits_before_the_next_line(self) -> None:
        for cmd in ("check", "apply"):
            with self.subTest(cmd=cmd), tempfile.TemporaryDirectory() as tmp:
                registry = self.registry(tmp, [{"name": "Fixture", "status": "archived"}])
                result = self.harness(registry, "Fixture", cmd)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("owner/Fixture is archived", result.stdout)
                self.assertIn(f"so {cmd} makes none and stops here", result.stdout)
                self.assertNotIn("REACHED_NEXT", result.stdout)

    def test_a_cataloged_entry_falls_through_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.registry(tmp, [{"name": "Fixture", "status": "cataloged"}])
            result = self.harness(registry, "Fixture", "check")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "REACHED_NEXT\n")

    def test_a_name_absent_from_the_registry_falls_through_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.registry(tmp, [{"name": "Other", "status": "archived"}])
            result = self.harness(registry, "Fixture", "check")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "REACHED_NEXT\n")

    def test_no_registry_file_falls_through_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.json"
            result = self.harness(missing, "Fixture", "check")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "REACHED_NEXT\n")

    def test_a_registry_that_will_not_parse_fails_the_run_rather_than_falling_through(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "repos.json"
            broken.write_text("not json", encoding="utf-8")
            result = self.harness(broken, "Fixture", "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("Failed to read status from", result.stderr)
            self.assertNotIn("REACHED_NEXT", result.stdout)

    def test_a_trailing_carriage_return_from_jq_is_stripped(self) -> None:
        """Windows' native jq -r appends \\r to its output, which a bare `=` compare would miss."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "jq"
            stub.write_text("#!/bin/sh\nprintf 'archived\\r\\n'\n", encoding="utf-8")
            stub.chmod(0o755)
            registry = self.registry(tmp, [{"name": "Fixture", "status": "cataloged"}])
            script = (
                f'PATH="$(cd {shlex.quote(str(bin_dir))} && pwd):$PATH"\n'
                f"registry={shlex.quote(str(registry))}\nname=Fixture\ncmd=check\nrepo=owner/Fixture\n"
                f"{REGISTRY_REGION}echo REACHED_NEXT\n"
            )
            result = run_bash(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("so check makes none and stops here", result.stdout)
            self.assertNotIn("REACHED_NEXT", result.stdout)


class UnregisteredApplyCase(RegistryRegionCase):
    """An apply for a repository the registry does not list fails before any write, and check does not."""

    def test_an_unlisted_name_fails_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.registry(tmp, [{"name": "Other", "status": "cataloged"}])
            result = self.harness(registry, "Fixture", "apply")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("owner/Fixture has no entry in", result.stderr)
            self.assertIn("develop only", result.stderr)
            self.assertNotIn("REACHED_NEXT", result.stdout)

    def test_no_registry_file_fails_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.json"
            result = self.harness(missing, "Fixture", "apply")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("not found, so apply makes no write", result.stderr)
            self.assertNotIn("REACHED_NEXT", result.stdout)

    def test_a_listed_name_falls_through_apply_whatever_its_letter_case(self) -> None:
        for listed in ("Fixture", "fixture"):
            with self.subTest(listed=listed), tempfile.TemporaryDirectory() as tmp:
                registry = self.registry(tmp, [{"name": listed, "status": "cataloged"}])
                result = self.harness(registry, "Fixture", "apply")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "REACHED_NEXT\n")

    def test_a_trailing_carriage_return_on_the_entry_count_is_stripped(self) -> None:
        """A count read as `0\\r` would not equal 0, so an unlisted apply would fall through."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "jq"
            stub.write_text("#!/bin/sh\nprintf '0\\r\\n'\n", encoding="utf-8")
            stub.chmod(0o755)
            registry = self.registry(tmp, [{"name": "Other", "status": "cataloged"}])
            script = (
                f'PATH="$(cd {shlex.quote(str(bin_dir))} && pwd):$PATH"\n'
                f"registry={shlex.quote(str(registry))}\nname=Fixture\ncmd=apply\nrepo=owner/Fixture\n"
                f"{REGISTRY_REGION}echo REACHED_NEXT\n"
            )
            result = run_bash(script)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("owner/Fixture has no entry in", result.stderr)
            self.assertNotIn("REACHED_NEXT", result.stdout)


class EndToEndCase(unittest.TestCase):
    """Run the real script, copied whole, with a `gh` stub that records every call.

    An archived target and an unregistered apply each exit before any `gh` call.
    """

    def run_configure(
        self, entries: list[dict[str, object]], args: list[str]
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        bash = require("bash", "jq")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo-config").mkdir()
            (root / "registry").mkdir()
            (root / "bin").mkdir()
            shutil.copy(CONFIGURE, root / "repo-config" / "configure.sh")
            (root / "registry" / "repos.json").write_text(
                json.dumps({"repos": entries}), encoding="utf-8"
            )
            calls = root / "gh-calls.txt"
            gh_stub = root / "bin" / "gh"
            gh_stub.write_text(
                f'#!/bin/sh\necho "$*" >> {shlex.quote(str(calls))}\nexit 1\n', encoding="utf-8"
            )
            gh_stub.chmod(0o755)
            env = dict(
                os.environ, PATH=f"{root / 'bin'}{os.pathsep}{os.environ.get('PATH', os.defpath)}"
            )
            result = subprocess.run(
                [bash, str(root / "repo-config" / "configure.sh"), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
                env=env,
            )
            return result, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def test_an_archived_target_never_calls_gh(self) -> None:
        for cmd in ("check", "apply"):
            with self.subTest(cmd=cmd):
                result, calls = self.run_configure(
                    [{"name": "Fixture", "status": "archived"}], [cmd, "owner/Fixture"]
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, "")

    def test_an_unregistered_apply_with_a_checkout_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as checkout:
            result, calls = self.run_configure(
                [{"name": "Other", "status": "cataloged"}],
                ["apply", "owner/Fixture", "release", "--checkout", checkout],
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("owner/Fixture has no entry in", result.stderr)
            self.assertEqual(calls, "")
            self.assertEqual(list(Path(checkout).iterdir()), [])


if __name__ == "__main__":
    unittest.main()

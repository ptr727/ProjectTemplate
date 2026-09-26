#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's archived-repository exemption by running its own lines.

The shell is lifted out of the file rather than restated here, so an edit that removes the
behavior fails these tests instead of leaving a reimplementation to agree with itself. The
region under test makes no `gh` call of its own, the repository it reads being resolved before it,
so the cases running it need no `gh` stub: a cataloged or absent registry falls through to a marker
printed just after the lifted region, and an archived one exits before reaching it.
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

CONFIGURE = Path(__file__).resolve().parents[2] / "repo-config" / "configure.sh"


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
    return str(shutil.which("bash"))


def run_bash(script: str, *tools: str) -> subprocess.CompletedProcess[str]:
    """The script under the same shell options configure.sh sets, with a bounded wait.

    The options are lifted rather than typed, because a harness running without them is blind to
    exactly the error handling the lines under test rely on.
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


ARCHIVED_EXEMPTION = lift(r'(if \[ -f "\$registry" \]; then\n    if ! archived_status=.*?\n^fi\n)')


class ArchivedRepositoryCase(unittest.TestCase):
    """A repository the registry marks archived is out of scope, for apply and check alike."""

    def registry(self, tmp: str, entries: list[dict[str, object]]) -> Path:
        path = Path(tmp) / "repos.json"
        path.write_text(json.dumps({"repos": entries}), encoding="utf-8")
        return path

    def harness(self, registry: Path, name: str, cmd: str) -> subprocess.CompletedProcess[str]:
        script = (
            f"registry={shlex.quote(str(registry))}\nname={shlex.quote(name)}\n"
            f"cmd={shlex.quote(cmd)}\nrepo={shlex.quote('owner/' + name)}\n"
            f"{ARCHIVED_EXEMPTION}echo REACHED_NEXT\n"
        )
        return run_bash(script, "jq")

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
            registry = self.registry(tmp, [{"name": "Fixture", "status": "archived"}])
            script = (
                f"PATH={shlex.quote(str(bin_dir))}:$PATH\n"
                f"registry={shlex.quote(str(registry))}\nname=Fixture\ncmd=check\nrepo=owner/Fixture\n"
                f"{ARCHIVED_EXEMPTION}echo REACHED_NEXT\n"
            )
            result = run_bash(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("so check makes none and stops here", result.stdout)
            self.assertNotIn("REACHED_NEXT", result.stdout)


class EndToEndCase(unittest.TestCase):
    """The real script, copied whole, exits before its first `gh` call, for apply and check alike."""

    def run_configure(self, status: str, cmd: str) -> subprocess.CompletedProcess[str]:
        bash = require("bash", "jq")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo-config").mkdir()
            (root / "registry").mkdir()
            (root / "bin").mkdir()
            shutil.copy(CONFIGURE, root / "repo-config" / "configure.sh")
            (root / "registry" / "repos.json").write_text(
                json.dumps({"repos": [{"name": "Fixture", "status": status}]}), encoding="utf-8"
            )
            gh_stub = root / "bin" / "gh"
            gh_stub.write_text(
                "#!/bin/sh\necho 'gh should never run for an archived repo' >&2\nexit 1\n",
                encoding="utf-8",
            )
            gh_stub.chmod(0o755)
            env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ.get('PATH', '')}")
            return subprocess.run(
                [bash, str(root / "repo-config" / "configure.sh"), cmd, "owner/Fixture"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
                env=env,
            )

    def test_an_archived_target_never_calls_gh(self) -> None:
        for cmd in ("check", "apply"):
            with self.subTest(cmd=cmd):
                result = self.run_configure("archived", cmd)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("gh should never run", result.stderr)


if __name__ == "__main__":
    unittest.main()

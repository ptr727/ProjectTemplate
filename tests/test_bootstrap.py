#!/usr/bin/env python3
"""Tests for the host bootstraps: the loader invariant, and that the tooling covers what the spec requires.

Two properties, both of which fail silently rather than loudly if nobody checks them.

The loader invariant is what keeps `host-setup/bootstrap.sh` and `host-setup/bootstrap.ps1` outside
the reach of the `Hub-Hosted Tooling` rule rather than exempt from it. A loader obtains a tree and
hands control to one entry point inside it. The moment it reads a second path in that tree it has
become a tool that reads hub content, and the rule applies to it in full. That boundary is a property
of each file, so it is asserted here rather than promised in prose.

The coverage assertion is the only connection between the floors in `spec/host-tools.json` and the
tooling that installs them. Nothing joins the two at runtime, deliberately: the gate measures a host
and the tooling changes one, and neither calls the other. Without a check at this level a tool could
be declared required and be one nothing here can install, which a host would discover as a gate it
cannot satisfy.

That assertion runs once per platform, because the two installers do not manage the same set and the
difference is a decision rather than an accident. `git-restore-mtime` serves a Linux deploy path and
the spec declares it not applicable on Windows.

Run as `python3 tests/test_bootstrap.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import TextIO, cast

from host_capability import bash_or_skip

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "host-setup" / "bootstrap.sh"
BOOTSTRAP_PS = ROOT / "host-setup" / "bootstrap.ps1"
MENU = ROOT / "host-setup" / "menu.sh"
LINUX = ROOT / "host-setup" / "linux"
WINDOWS = ROOT / "host-setup" / "windows"
HOST_TOOLS = ROOT / "spec" / "host-tools.json"

# The tools the linux tooling manages, read from the script rather than restated here, so the two cannot drift while both look correct.
TOOLS_DECLARATION = re.compile(r"^readonly TOOLS=\(([^)]*)\)", re.MULTILINE)

# The same, for the windows tooling, whose registry is a list of records rather than a flat array.
# The names are read out of the records themselves rather than from a second list beside them, so there is one declaration to keep true rather than two that can agree wrongly.
PS_TOOLS_OPEN = re.compile(r"^\$TOOLS\s*=\s*@\(", re.MULTILINE)
PS_TOOLS_CLOSE = re.compile(r"^\)", re.MULTILINE)
PS_TOOL_NAME = re.compile(r"^\s*@\{\s*Name\s*=\s*'([^']+)'", re.MULTILINE)

# A spec tool whose name differs from the name the installer knows it by, and why.
ALIASES = {
    "linux": {"python3": "python"},
    "windows": {"python3": "python"},
}

# A platform a floored tool's remedy deliberately omits, and the reason, so an omission is a decision rather than a hole in the mapping.
REMEDY_NOT_APPLICABLE = {
    "git-restore-mtime": {
        "windows": "The tool serves a Linux deploy path, which its source states."
    },
}

# The remedy commands that hand back into the host-setup installers, read so the tool each names can be checked against what that installer manages.
LINUX_INSTALLER_REMEDY = re.compile(r"^host-setup/linux/install-tools\.sh --upgrade (\S+)$")
WINDOWS_INSTALLER_REMEDY = re.compile(r"^host-setup/windows/install-tools\.ps1 -Upgrade (\S+)$")

RUNS_LINUX_INSTALLER = unittest.skipUnless(
    sys.platform == "linux", "executes install-tools.sh, which only a Linux host can run"
)

# A spec tool an installer deliberately does not manage, and the reason, recorded so an omission is a decision somebody made rather than one nobody noticed.
# Both sets are empty, which is itself the assertion: docker installs the same way on a hypervisor and a workstation on both platforms now, and the one case that differs, a WSL distribution, is handled inside install-tools.sh itself (it skips the native install and points at Docker Desktop's own WSL integration) rather than by leaving docker unmanaged on Linux entirely.
NOT_MANAGED: dict[str, dict[str, str]] = {
    "linux": {},
    "windows": {},
}


def tree_references(path: Path) -> set[str]:
    """The `$TREE/...` references a loader makes, deduplicated.

    Every reference to the fetched tree goes through the variable holding its location, so the
    paths a loader names are countable rather than scattered.
    """
    text = path.read_text(encoding="utf-8")
    references = re.findall(r'\$TREE(?:/[^"\'\s]*)?', text)
    return {reference for reference in references if "/" in reference}


class TestLoaderInvariant(unittest.TestCase):
    """`bootstrap.sh` and `bootstrap.ps1` each read exactly one path into the fetched tree.

    Both loaders write that one reference as a single interpolated, forward-slashed string
    (`$TREE/host-setup/<platform>/$tool` in each), rather than building it from parts, which is
    what lets one pattern check either file unmodified.
    """

    def _assert_reads_one_path(self, path: Path, expected: str) -> None:
        self.assertEqual(
            {expected},
            tree_references(path),
            f"{path.name} reads more than its one entry point into the fetched tree",
        )
        # A payload or a table read from the tree is what makes a file a tool rather than a loader.
        text = path.read_text(encoding="utf-8")
        for forbidden in ("spec/", "registry/", "repo-config/", "catalog/"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(
                    f"$TREE/{forbidden}",
                    text,
                    f"{path.name} reads {forbidden} from the fetched tree, which makes it a tool",
                )

    def test_linux_loader_reads_one_path_into_the_tree(self) -> None:
        """`bootstrap.sh` references exactly one directory inside the tree it fetches."""
        self._assert_reads_one_path(BOOTSTRAP, "$TREE/host-setup/linux/$tool")

    def test_windows_loader_reads_one_path_into_the_tree(self) -> None:
        """`bootstrap.ps1` references exactly one directory inside the tree it fetches.

        `$Tool`, PascalCase, because that is the parameter name PowerShell convention wants, where
        bash's equivalent is the lowercase local `$tool`. The pattern this shares with the Linux
        check is the shape of the path, one interpolated `$TREE/host-setup/<platform>/<name>`
        string, not the exact casing.
        """
        self._assert_reads_one_path(BOOTSTRAP_PS, "$TREE/host-setup/windows/$Tool")

    def _assert_needs_no_python(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        for interpreter in ("python3 ", "python ", "uv run", "py -3"):
            with self.subTest(interpreter=interpreter):
                self.assertNotIn(
                    interpreter,
                    text,
                    f"{path.name} invokes {interpreter.strip()}, which a host being bootstrapped may not have",
                )

    def test_linux_loader_needs_no_python(self) -> None:
        """A host `bootstrap.sh` stands up must not be made to install an interpreter first."""
        self._assert_needs_no_python(BOOTSTRAP)

    def test_windows_loader_needs_no_python(self) -> None:
        """A host `bootstrap.ps1` stands up must not be made to install an interpreter first."""
        self._assert_needs_no_python(BOOTSTRAP_PS)


class TestSpecCoverage(unittest.TestCase):
    """Every tool `spec/host-tools.json` requires is one the platform installers can provide."""

    def declared_tools(self) -> set[str]:
        """The tool names `install-tools.sh` manages."""
        text = (LINUX / "install-tools.sh").read_text(encoding="utf-8")
        match = TOOLS_DECLARATION.search(text)
        if match is None:
            self.fail("install-tools.sh declares no TOOLS array, so coverage cannot be checked")
        return {name.strip() for name in match.group(1).split() if name.strip()}

    def declared_windows_tools(self) -> set[str]:
        """The tool names `install-tools.ps1` manages."""
        text = (WINDOWS / "install-tools.ps1").read_text(encoding="utf-8")
        opened = PS_TOOLS_OPEN.search(text)
        if opened is None:
            self.fail(
                "install-tools.ps1 declares no $TOOLS registry, so coverage cannot be checked"
            )

        # A missing close marker is a failure rather than a scan to end of file.
        # Reading on past the registry collects every later `Name = '...'` in the script, so a broken registry answers with a larger tool set than it declares and the coverage check below passes on it.
        closed = PS_TOOLS_CLOSE.search(text, opened.end())
        if closed is None:
            self.fail(
                "install-tools.ps1 opens a $TOOLS registry this cannot find the end of, so coverage cannot be checked"
            )

        body = text[opened.end() : closed.start()]
        names = set(PS_TOOL_NAME.findall(body))
        if not names:
            self.fail(
                "install-tools.ps1 declares a $TOOLS registry with no Name fields this can read"
            )
        return names

    def spec_tools(self) -> list[dict]:
        """The tools the spec declares."""
        # A malformed or unreadable spec is a finding this reports beside the others, rather than a traceback that ends the run and takes the checks after it with it.
        try:
            return json.loads(HOST_TOOLS.read_text(encoding="utf-8"))["tools"]
        except (OSError, ValueError, KeyError) as error:
            self.fail(f"{HOST_TOOLS.name} could not be read as a tool declaration: {error}")

    def _assert_coverage(self, platform: str, managed: set[str], installer: str) -> None:
        """A tool the spec requires is one the named installer can provide, or a recorded exception."""
        if not managed:
            return

        for tool in self.spec_tools():
            name = tool["name"]
            if not tool.get("required", False):
                continue

            # Every required tool is checked, including one whose declaration names no source for this platform.
            # A tool is in scope because the spec requires it, never because its declaration happens to describe where this platform gets it.
            # Reading a missing `source.linux` as "not a Linux tool" skipped docker, git and uv, which is half the required set and the whole of what NOT_MANAGED exists to record.
            expected = ALIASES[platform].get(name, name)
            with self.subTest(tool=name):
                if name in NOT_MANAGED[platform]:
                    self.assertNotIn(
                        expected,
                        managed,
                        f"{name} is recorded as not managed on {platform}, but {installer} manages it, so the record is stale",
                    )
                    continue

                self.assertIn(
                    expected,
                    managed,
                    f"the spec requires {name} on {platform} and {installer} does not manage it, "
                    f"so a host cannot satisfy the gate by running the tooling",
                )

    def test_every_required_linux_tool_is_installable(self) -> None:
        """A tool the spec requires on Linux is one the tooling can provide, or a recorded exception."""
        self._assert_coverage("linux", self.declared_tools(), "install-tools.sh")

    def test_every_required_windows_tool_is_installable(self) -> None:
        """A tool the spec requires on Windows is one the tooling can provide, or a recorded exception."""
        self._assert_coverage("windows", self.declared_windows_tools(), "install-tools.ps1")

    def test_ripgrep_uses_the_platform_package_managers(self) -> None:
        """Ripgrep uses apt on Linux and the upstream project's documented winget package."""
        linux = (LINUX / "install-tools.sh").read_text(encoding="utf-8")
        windows = (WINDOWS / "install-tools.ps1").read_text(encoding="utf-8")
        install = re.search(
            r"ripgrep_install\(\) \{(?P<body>.*?)^\}", linux, re.MULTILINE | re.DOTALL
        )
        self.assertIsNotNone(install)
        body = install.group("body") if install else ""
        self.assertLess(body.index("ripgrep_remove_download"), body.index("apt_install ripgrep"))
        self.assertRegex(
            windows,
            r"Name = 'ripgrep'; Package = 'BurntSushi\.ripgrep\.MSVC'; Probe = 'rg'",
        )

    @RUNS_LINUX_INSTALLER
    def test_linux_installer_lists_a_repository_apt_package(self) -> None:
        """A repository package joins the managed set without becoming executable text."""
        declaration = {
            "tools": [
                {
                    "name": "virt-customize",
                    "install": {"linux": {"manager": "apt", "package": "libguestfs-tools"}},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "host-tools.json").write_text(json.dumps(declaration), encoding="utf-8")
            result = subprocess.run(
                [str(LINUX / "install-tools.sh"), "--list", "--repo", directory],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("virt-customize", result.stdout)
        self.assertIn("apt:libguestfs-tools (repository)", result.stdout)

    @RUNS_LINUX_INSTALLER
    def test_linux_installer_rejects_a_repository_collision(self) -> None:
        """Repository metadata cannot replace a fleet tool's specialized installer."""
        declaration = {
            "tools": [
                {
                    "name": "node",
                    "install": {"linux": {"manager": "apt", "package": "nodejs"}},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "host-tools.json").write_text(json.dumps(declaration), encoding="utf-8")
            result = subprocess.run(
                [str(LINUX / "install-tools.sh"), "--list", "--repo", directory],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot replace it", result.stderr)

    @RUNS_LINUX_INSTALLER
    def test_linux_installer_rejects_duplicate_repository_names(self) -> None:
        """Duplicate overlay names are ambiguous and fail before selection."""
        entry = {
            "name": "virt-customize",
            "install": {"linux": {"manager": "apt", "package": "libguestfs-tools"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "host-tools.json").write_text(
                json.dumps({"tools": [entry, entry]}), encoding="utf-8"
            )
            result = subprocess.run(
                [str(LINUX / "install-tools.sh"), "--list", "--repo", directory],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("more than once", result.stderr)

    @RUNS_LINUX_INSTALLER
    def test_linux_installer_rejects_an_unnamed_repository_tool(self) -> None:
        """Malformed applicable metadata fails instead of disappearing from the catalog."""
        declaration = {
            "tools": [{"install": {"linux": {"manager": "apt", "package": "libguestfs-tools"}}}]
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "host-tools.json").write_text(json.dumps(declaration), encoding="utf-8")
            result = subprocess.run(
                [str(LINUX / "install-tools.sh"), "--list", "--repo", directory],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot read constrained Linux install metadata", result.stderr)

    @RUNS_LINUX_INSTALLER
    def test_linux_installer_rejects_a_non_string_package(self) -> None:
        """JSON scalars do not become package identifiers through jq stringification."""
        declaration = {
            "tools": [
                {
                    "name": "virt-customize",
                    "install": {"linux": {"manager": "apt", "package": 7}},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "host-tools.json").write_text(json.dumps(declaration), encoding="utf-8")
            result = subprocess.run(
                [str(LINUX / "install-tools.sh"), "--list", "--repo", directory],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot read constrained Linux install metadata", result.stderr)

    @RUNS_LINUX_INSTALLER
    def test_linux_installer_rejects_extra_install_metadata(self) -> None:
        """The runtime trust boundary matches the schema's closed package object."""
        declaration = {
            "tools": [
                {
                    "name": "virt-customize",
                    "install": {
                        "linux": {
                            "manager": "apt",
                            "package": "libguestfs-tools",
                            "command": "do something",
                        }
                    },
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "host-tools.json").write_text(json.dumps(declaration), encoding="utf-8")
            result = subprocess.run(
                [str(LINUX / "install-tools.sh"), "--list", "--repo", directory],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot read constrained Linux install metadata", result.stderr)

    def test_sudo_timestamp_does_not_load_repository_tools(self) -> None:
        """The sudo-only action bypasses repository metadata before selection."""
        text = (LINUX / "install-tools.sh").read_text(encoding="utf-8")
        main = re.search(r"main\(\) \{(?P<body>.*?)^\}", text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(main)
        body = main.group("body") if main else ""
        self.assertRegex(body, r'if \[\[ \$MODE != "sudo-timestamp" \]\]; then\s+load_repo_tools')

    def test_windows_installer_requires_a_repository_tools_array(self) -> None:
        """The Windows trust-boundary reader rejects an object-shaped tool catalog."""
        text = (WINDOWS / "install-tools.ps1").read_text(encoding="utf-8")
        self.assertIn("$overlay.tools -isnot [System.Array]", text)

    def test_windows_installer_requires_string_package_metadata(self) -> None:
        """PowerShell cannot stringify JSON values before package-ID validation."""
        text = (WINDOWS / "install-tools.ps1").read_text(encoding="utf-8")
        self.assertIn("$metadata.manager -isnot [string]", text)
        self.assertIn("$metadata.package -isnot [string]", text)
        self.assertIn("$metadataKeys.Count -ne 2", text)

    def test_windows_installer_supplies_the_python3_name(self) -> None:
        """Windows has no `python3` of its own, so the installer both clears the lie and supplies the name.

        Two halves, and neither is sufficient alone. Windows ships app-execution alias stubs that
        answer to `python` and `python3` and only offer to open the Microsoft Store. They fail
        loudly, on stderr with exit 9009, so the harm is not a false pass but an occupied name: the
        alias directory is on `PATH` by default, so the stub answers wherever it sits ahead of the
        install directory. Removing them alone would leave `python3` simply absent on a host that
        does have Python, so the installer also puts a real one beside the interpreter it manages.
        The reparse-tag guard is what keeps the removal from deleting a real executable someone put
        in that directory.
        """
        text = (WINDOWS / "install-tools.ps1").read_text(encoding="utf-8")
        self.assertIn("function Repair-PythonName", text)
        self.assertIn("function Test-AppExecutionAlias", text)
        self.assertIn("0x8000001b", text)
        self.assertIn("$PYTHON_ALIAS_NAMES = @('python.exe', 'python3.exe')", text)
        self.assertIn("Repair-PythonName -Installed", text)

    def test_windows_python3_shim_names_no_version(self) -> None:
        """The shim follows whatever line the managed package installs, so its code hardcodes none.

        The package ID in `$TOOLS` is the one place a Python version is written down. A literal
        anywhere in the repair path would silently keep targeting the old line the day that ID moves,
        which is the failure mode this asserts against rather than merely documents.

        Comments are stripped before the match, because the invariant is about what the code
        resolves and not about what the prose may use as an example. The architecture-qualified tag
        rule is far clearer written as `3.13` beside `3.13-arm64` than described in the abstract, and
        a version named there cannot make a lookup target the wrong line.
        """
        text = (WINDOWS / "install-tools.ps1").read_text(encoding="utf-8")
        section = text.split("# --- Python ---", 1)
        self.assertEqual(len(section), 2, "install-tools.ps1 carries no Python section")
        body = section[1].split("# --- WSL ---", 1)[0]
        code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"Python3?\d\d|\d+\.\d+")
        self.assertIn("Get-PythonLine -Version $Installed", code)

    def test_every_declared_floor_carries_a_total_remedy_mapping(self) -> None:
        """Each floored tool names a runnable remedy on every platform, or carries a recorded exception.

        The gate prints the remedy under a below-floor failure, so a missing platform key is a
        failure that tells the operator to upgrade and not how. A remedy that hands back into an
        installer here is also checked to name a tool that installer manages, so the command it
        prints can actually run.
        """
        linux_managed = self.declared_tools()
        windows_managed = self.declared_windows_tools()
        for tool in self.spec_tools():
            name = tool["name"]
            if tool.get("minimum") is None:
                continue
            remedy = tool.get("remedy")
            with self.subTest(tool=name):
                if not isinstance(remedy, dict) or not remedy:
                    self.fail(
                        f"{name} declares a floor and no remedy, so its failure names no command"
                    )
                for platform in ("linux", "macos", "windows"):
                    if platform in REMEDY_NOT_APPLICABLE.get(name, {}):
                        self.assertNotIn(
                            platform,
                            remedy,
                            f"{name} is recorded as not applicable on {platform} and carries a remedy there, so the record is stale",
                        )
                        continue
                    command = remedy.get(platform)
                    self.assertTrue(
                        isinstance(command, str) and bool(command),
                        f"{name} declares a floor and no {platform} remedy, so a below-floor host there is told to upgrade and not how",
                    )
                installer_cases = (
                    ("linux", LINUX_INSTALLER_REMEDY, linux_managed, "install-tools.sh"),
                    ("windows", WINDOWS_INSTALLER_REMEDY, windows_managed, "install-tools.ps1"),
                )
                for platform, pattern, managed, installer in installer_cases:
                    command = remedy.get(platform)
                    if not isinstance(command, str):
                        continue
                    match = pattern.match(command)
                    if match and managed:
                        self.assertIn(
                            match.group(1),
                            managed,
                            f"{name} remedy.{platform} names {match.group(1)}, which {installer} does not manage, so the printed command fails",
                        )


class TestScriptPresence(unittest.TestCase):
    """Every script a loader hands control to is present and, on Linux, executable."""

    def indexed_modes(self) -> dict[str, str]:
        """The file modes git records, keyed by repo-relative path.

        Git's mode is read rather than the filesystem's, because the filesystem does not carry one
        on every platform this runs on. NTFS has no exec bit, so `st_mode` reports every file as
        non-executable and the assertion below fails on Windows against a tree that is correct.
        What the loader actually depends on is the mode a Linux checkout gets, and that is the one
        git stores.
        """
        try:
            listing = subprocess.run(
                ["git", "ls-files", "-s", "--", "host-setup"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                check=True,
                cwd=ROOT,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as error:
            self.fail(
                f"git could not report the recorded file modes, so executability is unchecked: {error}"
            )

        modes: dict[str, str] = {}
        for line in listing.splitlines():
            # Each row is "<mode> <object> <stage>\t<path>", so the tab is what separates the fields from the path.
            fields, _, path = line.partition("\t")
            if path:
                modes[path] = fields.split()[0]
        return modes

    def test_every_managed_tool_is_executable(self) -> None:
        """Each script the loader hands control to is present and executable."""
        modes = self.indexed_modes()
        for name in ("install-skills.sh", "install-tools.sh", "upgrade-host.sh", "setup-github.sh"):
            path = LINUX / name
            with self.subTest(script=name):
                self.assertTrue(path.is_file(), f"{name} is missing from host-setup/linux")
                if path.is_file():
                    mode = modes.get(f"host-setup/linux/{name}", "")
                    self.assertEqual(
                        "100755",
                        mode,
                        f"{name} is recorded as {mode or 'untracked'} rather than 100755, "
                        f"so a fresh checkout cannot run it",
                    )

    def test_every_windows_script_is_present(self) -> None:
        """Each Windows script is present, and none opens with a shebang.

        The exec bit is the Linux form of "this will run", and on Windows the equivalent property
        is the absence of a shebang: `scripts/repo_gate.py --check eol-coverage` requires git to
        resolve any tracked file opening `#!` to `eol=lf` via a `.gitattributes` pin, and these
        files carry none of their own. A shebang added later would fail that gate from a file
        nobody would think to look at.
        """
        scripts = (
            "install-skills.ps1",
            "install-tools.ps1",
            "upgrade-host.ps1",
            "setup-github.ps1",
            "setup-wsl.ps1",
        )
        for name in scripts + ("README.md",):
            with self.subTest(file=name):
                self.assertTrue(
                    (WINDOWS / name).is_file(), f"{name} is missing from host-setup/windows"
                )

        for name in scripts:
            path = WINDOWS / name
            if path.is_file():
                with self.subTest(script=name):
                    self.assertFalse(
                        path.read_bytes().startswith(b"#!"),
                        f"{name} opens with a shebang, which the eol-coverage gate then requires "
                        f"a `.gitattributes` pin for",
                    )

    def test_bootstrap_ps1_is_present_and_unmarked(self) -> None:
        """`bootstrap.ps1` is present, and does not open with a shebang.

        Kept apart from `test_every_windows_script_is_present` rather than folded into it, because
        `bootstrap.ps1` deliberately sits beside `bootstrap.sh` at `host-setup/`, not inside
        `host-setup/windows/` with the four scripts that test checks. Same reasoning as that test:
        the `eol-coverage` gate requires a `.gitattributes` pin for any tracked file opening `#!`,
        and this file carries none of its own.
        """
        self.assertTrue(BOOTSTRAP_PS.is_file(), "bootstrap.ps1 is missing from host-setup")
        if BOOTSTRAP_PS.is_file():
            self.assertFalse(
                BOOTSTRAP_PS.read_bytes().startswith(b"#!"),
                "bootstrap.ps1 opens with a shebang, which the eol-coverage gate then requires a "
                "`.gitattributes` pin for",
            )

    def test_bootstrap_ps1_names_the_system32_tar(self) -> None:
        """`bootstrap.ps1` reaches tar only through `Get-TarPath`, which names the System32 copy.

        A pwsh launched from Git Bash finds MSYS tar first on `PATH`, and that tar reads the drive
        prefix of a Windows archive path as a remote host, so a bare `tar` makes the extraction
        depend on the shell the loader was started from. A path to any other tar, Git's own
        included, reaches the same MSYS tar without `PATH`. The check is an allow-list over every
        code line that names tar, so it holds whatever form the invocation on such a line takes.
        A `tar.gz` is not tar named only as an archive extension (`name.tar.gz`) or as a URL path
        segment (`/tar.gz/`), neither of which can name an executable.
        """
        text = BOOTSTRAP_PS.read_text(encoding="utf-8")
        definition = "function Get-TarPath { Join-Path $env:SystemRoot 'System32\\tar.exe' }"
        self.assertIn(definition, text.splitlines())
        names_tar = re.compile(
            r"(?<=\.)tar\b(?!\.gz(?![\w.]))|(?<!\.)\btar\b(?!\.gz/)", re.IGNORECASE
        )
        message = re.compile(r"^die '[^']*'$")
        strays = [
            line.strip()
            for line in text.splitlines()
            if not line.lstrip().startswith("#")
            and names_tar.search(line)
            and line.strip() not in (definition, "")
            and not message.match(line.strip())
        ]
        self.assertEqual(strays, [], "bootstrap.ps1 names a tar other than through Get-TarPath")


def bootstrap_functions() -> str:
    """`bootstrap.sh` without its closing `main "$@"`, so a test can source its functions alone."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").rstrip("\n").split("\n")
    if lines[-1] != 'main "$@"':
        raise AssertionError(f"bootstrap.sh no longer ends with its main call: {lines[-1]!r}")
    return "\n".join(lines[:-1]) + "\n"


REGISTRATION_RECORDER = """\
import os
from pathlib import Path

Path(os.environ["REGISTERED_RECORD"]).write_text(str(Path(__file__).resolve().parent.parent), encoding="utf-8")
"""


def installer_tree(root: Path, platform: str, wrapper: str) -> Path:
    """A tree holding the real skills wrapper for `platform`, its installer replaced by one recording the ROOT it would register."""
    target = root / "host-setup" / platform / wrapper
    target.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "host-setup" / platform / wrapper, target)
    recorder = root / "scripts" / "skills_install.py"
    recorder.parent.mkdir()
    recorder.write_text(REGISTRATION_RECORDER, encoding="utf-8")
    return root


class TestDirectoryLockOrder(unittest.TestCase):
    """A run refused the directory lock stops before anything that removes a tree.

    Each loader's cleanup removes trees by their fixed names, so a refused run that reached it would
    delete the trees of the very run holding the lock.
    """

    def test_the_linux_loader_locks_before_setting_its_exit_trap(self) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        main = text[text.index("\nmain() {") :]
        self.assertLess(main.index("\n    lock_dir\n"), main.index("trap cleanup EXIT"))

    def test_the_windows_loader_locks_before_the_try_its_cleanup_runs_from(self) -> None:
        text = BOOTSTRAP_PS.read_text(encoding="utf-8")
        main = text[text.index("\nfunction main {") :]
        self.assertLess(main.index("\n    Lock-Directory\n"), main.index("\n    try {\n"))


def hold_flock(path: Path) -> TextIO:
    """Holds `path` the way `bootstrap.sh` does, through the flock(2) its flock command takes."""
    import fcntl

    handle = path.open("a", encoding="utf-8")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


@unittest.skipUnless(sys.platform == "linux", "drives the Linux loader's own functions")
class TestKeptTreeHandling(unittest.TestCase):
    """The Linux loader's removal and swap of the trees it owns, driven through its own functions."""

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.functions = self.dir / "functions.sh"
        self.functions.write_text(bootstrap_functions(), encoding="utf-8")
        self.addCleanup(self._unlock_all)

    def _unlock_all(self) -> None:
        for path in self.dir.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)

    def run_loader(self, body: str) -> subprocess.CompletedProcess[str]:
        script = f'source "{self.functions}"\nDIR="{self.dir}"\nMODE=skills\n{body}\n'
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )

    def owned_tree(self, name: str, content: str) -> Path:
        tree = self.dir / name
        tree.mkdir()
        (tree / ".bootstrap-owned").touch()
        (tree / "content").write_text(content, encoding="utf-8")
        return tree

    def locked_entry(self, tree: Path) -> None:
        """An entry `rm` cannot remove, standing in for a removal that stops part way."""
        if os.geteuid() == 0:
            self.skipTest("root removes a read-only directory's entries regardless of its mode")
        locked = tree / "locked"
        locked.mkdir()
        (locked / "file").touch()
        locked.chmod(0o555)

    def test_a_removal_that_stops_part_way_keeps_the_ownership_marker(self) -> None:
        """A leftover without its marker is refused by every later swap, with a remedy that is wrong."""
        tree = self.owned_tree("skills-tree.new", "new")
        self.locked_entry(tree)
        result = self.run_loader(f'remove_tree "{tree}"')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse((tree / "content").exists(), result.stderr)
        self.assertTrue((tree / ".bootstrap-owned").exists(), result.stderr)

    def test_a_dangling_symlink_is_refused_with_the_loaders_own_message(self) -> None:
        (self.dir / "skills-tree.new").symlink_to(self.dir / "nowhere")
        result = self.run_loader('remove_owned "$(staging_path)"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("this loader did not create it", result.stderr)

    def test_an_old_tree_beside_an_empty_name_does_not_stop_the_new_one_landing(self) -> None:
        self.owned_tree("skills-tree.new", "new")
        self.locked_entry(self.owned_tree("skills-tree.old", "old"))
        result = self.run_loader("swap_in")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dir / "skills-tree" / "content").read_text(encoding="utf-8"), "new")

    def test_a_leftover_old_tree_that_will_not_go_stops_the_swap_naming_its_path(self) -> None:
        self.owned_tree("skills-tree.new", "new")
        self.owned_tree("skills-tree", "live")
        self.locked_entry(self.owned_tree("skills-tree.old", "old"))
        result = self.run_loader("swap_in")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"Could not remove the previous tree at {self.dir / 'skills-tree.old'}", result.stderr
        )
        self.assertEqual((self.dir / "skills-tree" / "content").read_text(encoding="utf-8"), "live")

    def test_a_failed_swap_never_moves_a_foreign_old_tree_into_place(self) -> None:
        self.owned_tree("skills-tree.new", "new")
        foreign = self.dir / "skills-tree.old"
        foreign.mkdir()
        result = self.run_loader(
            'mv() { [[ $1 == *.new ]] && return 1; command mv "$@"; }\nswap_in'
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(foreign.is_dir())
        self.assertFalse((self.dir / "skills-tree").exists())

    def test_cleanup_restores_the_old_tree_even_where_the_staging_tree_will_not_go(self) -> None:
        self.locked_entry(self.owned_tree("skills-tree.new", "new"))
        self.owned_tree("skills-tree.old", "old")
        result = self.run_loader("cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dir / "skills-tree" / "content").read_text(encoding="utf-8"), "old")

    def test_cleanup_from_a_successful_runs_exit_trap_warns_where_a_leftover_old_tree_will_not_go(
        self,
    ) -> None:
        self.owned_tree("skills-tree", "live")
        self.locked_entry(self.owned_tree("skills-tree.old", "old"))
        result = self.run_loader("trap cleanup EXIT\nexit 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Could not remove the previous tree", result.stderr)

    def test_a_transient_tree_that_will_not_go_does_not_fail_a_successful_run(self) -> None:
        self.locked_entry(self.owned_tree("tree", "fetched"))
        result = self.run_loader("MODE=report\ntrap cleanup EXIT\nexit 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Could not remove the fetched tree", result.stderr)

    def test_cleanup_keeps_the_old_tree_where_the_name_holds_something_not_ours(self) -> None:
        (self.dir / "skills-tree").symlink_to(self.dir / "elsewhere")
        self.owned_tree("skills-tree.old", "old")
        result = self.run_loader("cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.dir / "skills-tree.old" / "content").read_text(encoding="utf-8"), "old"
        )

    def test_cleanup_puts_the_old_tree_back_where_a_swap_left_the_name_empty(self) -> None:
        self.owned_tree("skills-tree.old", "old")
        result = self.run_loader("cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dir / "skills-tree" / "content").read_text(encoding="utf-8"), "old")

    def test_a_swap_that_cannot_land_the_new_tree_puts_the_live_one_back(self) -> None:
        self.owned_tree("skills-tree.new", "new")
        self.owned_tree("skills-tree", "live")
        result = self.run_loader(
            'mv() { [[ $1 == *.new ]] && return 1; command mv "$@"; }\nswap_in'
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not move the extracted tree into place", result.stderr)
        self.assertEqual((self.dir / "skills-tree" / "content").read_text(encoding="utf-8"), "live")
        self.assertFalse((self.dir / "skills-tree.old").exists())

    def test_a_swap_that_cannot_put_the_live_tree_back_names_where_it_is(self) -> None:
        self.owned_tree("skills-tree.new", "new")
        self.owned_tree("skills-tree", "live")
        result = self.run_loader(
            'mv() { [[ $1 == *.new || $1 == *.old ]] && return 1; command mv "$@"; }\nswap_in'
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"could not put the previous tree back from {self.dir / 'skills-tree.old'}",
            result.stderr,
        )
        self.assertEqual(
            (self.dir / "skills-tree.old" / "content").read_text(encoding="utf-8"), "live"
        )

    def test_a_run_is_refused_while_another_holds_the_directory_lock(self) -> None:
        handle = hold_flock(self.dir / "skills-tree.lock")
        self.addCleanup(handle.close)
        result = self.run_loader("lock_dir")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another bootstrap run is using", result.stderr)

    def test_the_directory_lock_is_free_once_the_run_holding_it_ends(self) -> None:
        for _ in range(2):
            result = self.run_loader("lock_dir")
            self.assertEqual(result.returncode, 0, result.stderr)
        hold_flock(self.dir / "skills-tree.lock").close()

    def test_a_tool_the_loader_runs_does_not_inherit_the_lock(self) -> None:
        """A daemon a tool starts would otherwise hold the lock, and refuse every later run, long after this one."""
        tool = self.dir / "tree" / "host-setup" / "linux" / "probe.sh"
        tool.parent.mkdir(parents=True)
        tool.write_text(
            "#!/usr/bin/env bash\nif [[ -e /proc/self/fd/$1 ]]; then echo inherited; else echo closed; fi\n",
            encoding="utf-8",
        )
        tool.chmod(0o755)
        result = self.run_loader(
            f'lock_dir\nTREE="{self.dir / "tree"}"\nrun_tool probe.sh "$LOCK_FD"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "closed")

    def test_an_empty_directory_at_a_managed_name_is_removed_rather_than_refused(self) -> None:
        """What a removal leaves where only the directory itself would not go, its marker already gone."""
        (self.dir / "skills-tree.new").mkdir()
        result = self.run_loader('remove_owned "$(staging_path)"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.dir / "skills-tree.new").exists())

    def test_an_empty_directory_at_the_trees_name_is_replaced_by_the_new_tree(self) -> None:
        (self.dir / "skills-tree").mkdir()
        self.owned_tree("skills-tree.new", "new")
        result = self.run_loader("swap_in")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dir / "skills-tree" / "content").read_text(encoding="utf-8"), "new")
        self.assertFalse((self.dir / "skills-tree.old").exists())

    def test_a_directory_that_cannot_be_listed_is_not_taken_for_an_empty_one(self) -> None:
        """Its contents are unknown, so it is refused as somebody else's rather than moved aside as ours."""
        if os.geteuid() == 0:
            self.skipTest("root lists a directory regardless of its mode")
        foreign = self.dir / "skills-tree"
        foreign.mkdir()
        (foreign / "theirs").write_text("theirs", encoding="utf-8")
        foreign.chmod(0o000)
        self.owned_tree("skills-tree.new", "new")
        result = self.run_loader("swap_in")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("this loader did not create it", result.stderr)
        self.assertFalse((self.dir / "skills-tree.old").exists())

    def test_an_unmarked_directory_holding_anything_is_still_refused(self) -> None:
        foreign = self.dir / "skills-tree.new"
        foreign.mkdir()
        (foreign / "theirs").write_text("theirs", encoding="utf-8")
        result = self.run_loader('remove_owned "$(staging_path)"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("this loader did not create it", result.stderr)
        self.assertTrue((foreign / "theirs").exists())

    def test_the_directory_the_skills_installer_registers_outlives_the_run(self) -> None:
        """The marketplace loads the directory the installer ran from, so that has to be the tree the run keeps."""
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        top = installer_tree(work / "ProjectTemplate-0123456", "linux", "install-skills.sh")
        archive = work / "archive.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(top, arcname=top.name)
        record = work / "registered"
        result = self.run_loader(
            f'export REGISTERED_RECORD="{record}"\n'
            f'fetch() {{ cp "{archive}" "$2"; }}\n'
            "RESOLVED=0123456789abcdef0123456789abcdef01234567\n"
            "lock_dir\ntrap cleanup EXIT\ndownload_tree\ninstall_skills"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        registered = Path(record.read_text(encoding="utf-8"))
        self.assertEqual(registered, (self.dir / "skills-tree").resolve())
        self.assertTrue((registered / "scripts" / "skills_install.py").is_file())
        self.assertFalse((self.dir / "skills-tree.new").exists())
        self.assertFalse((self.dir / "skills-tree.old").exists())


POWERSHELL_TREE_HARNESS = r"""
param([string]$Loader, [string]$Dir, [string]$BodyFile)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Loader, [ref]$tokens, [ref]$errors)
$wanted = @(
    'log', 'info', 'step', 'warn', 'die',
    'Test-KeepsTree', 'Get-TreeName', 'Get-TreePath', 'Get-StagingPath', 'Get-RetiredPath', 'Get-ArchivePath',
    'Get-LockPath', 'Lock-Directory', 'Test-Ownership', 'Remove-Owned', 'Get-Tree', 'Remove-Tree', 'Move-Tree', 'Invoke-SwapIn', 'Invoke-Cleanup',
    'Resolve-Directory', 'Invoke-Tool', 'Invoke-SkillsInstall'
)
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$script:DIR = $Dir
$script:MODE = 'skills'
$script:DRY_RUN = $false
$script:ASSUME_YES = $false
$script:KEEP = $false
$script:PWSH_PATH = (Get-Process -Id $PID).Path
$script:REPO = 'ptr727/ProjectTemplate'
$script:REF = 'main'
$script:RESOLVED = ''
$script:TREE = ''
$script:LOCK = $null
$script:HELD = [Collections.Generic.List[object]]::new()
function Get-Named { param([string]$Name) Join-Path $script:DIR $Name }
# Holds an entry the way a process using the tree does: an open handle on Windows, where that alone stops a rename and a delete, and a read-only directory elsewhere, where only the delete stops.
function Lock-Entry {
    param([string]$Name)
    $locked = Join-Path (Get-Named $Name) 'locked'
    New-Item -ItemType Directory -Path $locked -Force | Out-Null
    New-Item -ItemType File -Path (Join-Path $locked 'file') -Force | Out-Null
    if ($IsWindows) {
        $script:HELD.Add([IO.File]::Open((Join-Path $locked 'file'), 'Open', 'Read', 'None'))
    } else {
        & chmod 555 $locked
    }
}
# Holds one file directly in a tree open, which only Windows can do without also locking the tree's own entries, the marker among them.
function Hold-File {
    param([string]$Name, [string]$File)
    $path = Join-Path (Get-Named $Name) $File
    New-Item -ItemType File -Path $path -Force | Out-Null
    $script:HELD.Add([IO.File]::Open($path, 'Open', 'Read', 'None'))
}
# A link at a tree's name, a junction on Windows since a symbolic link there needs a privilege a test host may not hold.
function New-Link {
    param([string]$Name, [string]$Target)
    $type = if ($IsWindows) { 'Junction' } else { 'SymbolicLink' }
    New-Item -ItemType $type -Path (Get-Named $Name) -Target (Get-Named $Target) | Out-Null
}
# Fails the move of any tree whose name ends in one of $Suffixes, and moves every other one as the loader does.
function Set-MoveFailure {
    param([string[]]$Suffixes)
    $script:FAIL_MOVE = $Suffixes
    function script:Move-Tree {
        param([string]$Path, [string]$Destination)
        foreach ($suffix in $script:FAIL_MOVE) { if ($Path.EndsWith($suffix)) { throw "refused to move $Path" } }
        [IO.Directory]::Move($Path, $Destination)
    }
}
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $BodyFile)))
"""


@unittest.skipUnless(shutil.which("pwsh"), "needs pwsh to drive the Windows loader's own functions")
class TestPowerShellKeptTreeHandling(unittest.TestCase):
    """The Windows loader's removal and swap of the trees it owns, driven through its own functions.

    Each case runs `bootstrap.ps1`'s own functions, extracted by AST, against a scratch directory.
    An entry a case locks is held for real, by an open handle on Windows, so the rename and delete
    failures the loader is shaped around are the host's own rather than a stub's.
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(self._unlock_all)

    def _unlock_all(self) -> None:
        if sys.platform == "win32":
            return
        for path in self.dir.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)

    def loader_command(self, directory: Path, body: str) -> list[str]:
        harness = directory / "harness.ps1"
        harness.write_text(POWERSHELL_TREE_HARNESS, encoding="utf-8")
        body_file = directory / "body.ps1"
        body_file.write_text(body, encoding="utf-8")
        return [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            "-Loader",
            str(BOOTSTRAP_PS),
            "-Dir",
            str(self.dir),
            "-BodyFile",
            str(body_file),
        ]

    def run_loader(
        self, body: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Runs `body` in the harness, `env` reaching it as environment so a path is never quoted into the script."""
        with tempfile.TemporaryDirectory() as directory:
            return subprocess.run(
                self.loader_command(Path(directory), body),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
                env={**os.environ, **(env or {})},
            )

    def hold_lock(self) -> subprocess.Popen[str]:
        """A second pwsh holding the directory lock through the loader's own Lock-Directory, until its input closes."""
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        holder = subprocess.Popen(
            self.loader_command(
                directory,
                "Lock-Directory\n[Console]::Out.WriteLine('held')\n[void][Console]::In.ReadLine()",
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.addCleanup(self._end_holder, holder)
        output = holder.stdout
        assert output is not None
        lines: list[str] = []
        reader = threading.Thread(target=lambda: lines.append(output.readline()))
        reader.start()
        reader.join(timeout=60)
        if reader.is_alive() or lines != ["held\n"]:
            holder.kill()
            reader.join(timeout=10)
            self.fail(f"the holder never took the lock: {lines!r}")
        return holder

    @staticmethod
    def _end_holder(holder: subprocess.Popen[str]) -> None:
        if holder.poll() is None:
            holder.kill()
        holder.communicate(timeout=60)

    def owned_tree(self, name: str, content: str) -> Path:
        tree = self.dir / name
        tree.mkdir()
        (tree / ".bootstrap-owned").touch()
        (tree / "content").write_text(content, encoding="utf-8")
        return tree

    def lock(self, name: str) -> str:
        """The harness line that holds an entry in the tree `name`, skipped where nothing can."""
        if sys.platform != "win32" and os.geteuid() == 0:
            self.skipTest("root removes a read-only directory's entries regardless of its mode")
        return f"Lock-Entry '{name}'"

    def content(self, name: str) -> str:
        return (self.dir / name / "content").read_text(encoding="utf-8")

    @unittest.skipUnless(
        sys.platform == "win32",
        "a plain recursive removal reaches the marker before a held entry only where a lone file can be held",
    )
    def test_a_removal_that_stops_part_way_keeps_the_ownership_marker(self) -> None:
        """A held file sorting after the marker, which a plain recursive removal would reach second."""
        self.owned_tree("skills-tree.new", "new")
        result = self.run_loader(
            "Hold-File 'skills-tree.new' 'zz-held'\nRemove-Tree -Path (Get-Named 'skills-tree.new')"
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.dir / "skills-tree.new" / "content").exists(), result.stderr)
        self.assertTrue((self.dir / "skills-tree.new" / ".bootstrap-owned").exists())

    def test_a_link_to_an_owned_tree_is_not_ours_and_is_refused(self) -> None:
        self.owned_tree("elsewhere", "kept")
        result = self.run_loader(
            "New-Link 'skills-tree.new' 'elsewhere'\nRemove-Owned -Path (Get-StagingPath)"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("this loader did not create it", result.stderr)
        self.assertEqual(self.content("elsewhere"), "kept")

    def test_an_old_tree_beside_an_empty_name_does_not_stop_the_new_one_landing(self) -> None:
        self.owned_tree("skills-tree.new", "new")
        self.owned_tree("skills-tree.old", "old")
        result = self.run_loader(f"{self.lock('skills-tree.old')}\nInvoke-SwapIn")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.content("skills-tree"), "new")
        self.assertIn("Could not remove the previous tree", result.stderr)

    def test_a_leftover_old_tree_that_will_not_go_stops_the_swap_naming_its_path(self) -> None:
        self.owned_tree("skills-tree.new", "new")
        self.owned_tree("skills-tree", "live")
        self.owned_tree("skills-tree.old", "old")
        result = self.run_loader(f"{self.lock('skills-tree.old')}\nInvoke-SwapIn")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"Could not remove the previous tree at {self.dir / 'skills-tree.old'}", result.stderr
        )
        self.assertEqual(self.content("skills-tree"), "live")

    def test_a_swap_that_cannot_land_the_new_tree_puts_the_live_one_back(self) -> None:
        self.owned_tree("skills-tree.new", "new")
        self.owned_tree("skills-tree", "live")
        result = self.run_loader("Set-MoveFailure '.new'\nInvoke-SwapIn")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not move the extracted tree into place", result.stderr)
        self.assertEqual(self.content("skills-tree"), "live")
        self.assertFalse((self.dir / "skills-tree.old").exists())

    def test_a_swap_that_cannot_put_the_live_tree_back_names_where_it_is(self) -> None:
        self.owned_tree("skills-tree.new", "new")
        self.owned_tree("skills-tree", "live")
        result = self.run_loader("Set-MoveFailure '.new', '.old'\nInvoke-SwapIn")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"could not put the previous tree back from {self.dir / 'skills-tree.old'}",
            result.stderr,
        )
        self.assertEqual(self.content("skills-tree.old"), "live")

    def test_a_failed_swap_never_moves_a_foreign_old_tree_into_place(self) -> None:
        self.owned_tree("skills-tree.new", "new")
        foreign = self.dir / "skills-tree.old"
        foreign.mkdir()
        result = self.run_loader("Set-MoveFailure '.new'\nInvoke-SwapIn")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(foreign.is_dir())
        self.assertFalse((self.dir / "skills-tree").exists())

    def test_cleanup_restores_the_old_tree_even_where_the_staging_tree_will_not_go(self) -> None:
        self.owned_tree("skills-tree.new", "new")
        self.owned_tree("skills-tree.old", "old")
        result = self.run_loader(f"{self.lock('skills-tree.new')}\nInvoke-Cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Could not remove the extracted tree", result.stderr)
        self.assertEqual(self.content("skills-tree"), "old")

    def test_cleanup_warns_where_a_leftover_old_tree_will_not_go(self) -> None:
        self.owned_tree("skills-tree", "live")
        self.owned_tree("skills-tree.old", "old")
        result = self.run_loader(f"{self.lock('skills-tree.old')}\nInvoke-Cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Could not remove the previous tree", result.stderr)

    def test_a_transient_tree_that_will_not_go_does_not_fail_a_successful_run(self) -> None:
        self.owned_tree("tree", "fetched")
        result = self.run_loader(f"$script:MODE = 'report'\n{self.lock('tree')}\nInvoke-Cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Could not remove the fetched tree", result.stderr)

    def test_cleanup_keeps_the_old_tree_where_the_name_holds_something_not_ours(self) -> None:
        self.owned_tree("elsewhere", "kept")
        self.owned_tree("skills-tree.old", "old")
        result = self.run_loader("New-Link 'skills-tree' 'elsewhere'\nInvoke-Cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.content("skills-tree.old"), "old")

    def test_cleanup_puts_the_old_tree_back_where_a_swap_left_the_name_empty(self) -> None:
        self.owned_tree("skills-tree.old", "old")
        result = self.run_loader("Invoke-Cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.content("skills-tree"), "old")

    def test_a_failed_move_surfaces_the_file_system_error_rather_than_its_wrapper(self) -> None:
        result = self.run_loader(
            "try { Move-Tree -Path (Get-Named 'missing') -Destination (Get-Named 'skills-tree') }"
            " catch { [Console]::Error.WriteLine($_.Exception.GetType().FullName) }"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("System.IO.DirectoryNotFoundException", result.stderr)

    @unittest.skipUnless(
        sys.platform == "win32", "only Windows has a path that is rooted but not fully qualified"
    )
    def test_a_dir_that_is_rooted_but_not_fully_qualified_is_refused(self) -> None:
        """A drive-relative or root-relative path resolves against a directory nothing else reads."""
        for given in ("C:hs", "\\hs"):
            with self.subTest(given=given):
                result = self.run_loader(f"$script:Dir = '{given}'\nResolve-Directory")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("-Dir takes an absolute path", result.stderr)

    def fetch(self, tar: str, then: str = "Invoke-SwapIn") -> str:
        """Harness lines that stand in for the download and the extract, then run a kept-tree fetch, `then`, and the cleanup."""
        return (
            "function Invoke-WebRequest { param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile, [int]$TimeoutSec) Set-Content -LiteralPath $OutFile -Value 'archive' }\n"
            f"function Invoke-FakeTar {{ {tar} }}\n"
            "function Get-TarPath { 'Invoke-FakeTar' }\n"
            "$script:RESOLVED = '0123456789abcdef0123456789abcdef01234567'\n"
            f"try {{ Get-Tree; {then} }} finally {{ Invoke-Cleanup }}"
        )

    def test_a_kept_tree_fetch_swaps_the_new_tree_in_and_records_its_commit(self) -> None:
        self.owned_tree("skills-tree", "live")
        extract = (
            "$into = $args[[array]::IndexOf($args, '-C') + 1]; "
            "Set-Content -LiteralPath (Join-Path $into 'content') -Value 'new' -NoNewline; "
            "$global:LASTEXITCODE = 0"
        )
        result = self.run_loader(self.fetch(extract))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.content("skills-tree"), "new")
        self.assertTrue((self.dir / "skills-tree" / ".bootstrap-owned").exists())
        self.assertEqual(
            (self.dir / "skills-tree" / ".bootstrap-commit").read_text(encoding="ascii").strip(),
            "0123456789abcdef0123456789abcdef01234567",
        )
        self.assertEqual(sorted(path.name for path in self.dir.iterdir()), ["skills-tree"])

    def test_the_directory_the_skills_installer_registers_outlives_the_run(self) -> None:
        """The marketplace loads the directory the installer ran from, so that has to be the tree the run keeps."""
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = installer_tree(work / "source", "windows", "install-skills.ps1")
        record = work / "registered"
        extract = (
            "$into = $args[[array]::IndexOf($args, '-C') + 1]; "
            "Copy-Item -Path (Join-Path $env:FIXTURE_SOURCE '*') -Destination $into -Recurse; "
            "$global:LASTEXITCODE = 0"
        )
        result = self.run_loader(
            self.fetch(extract, then="Invoke-SkillsInstall"),
            env={"FIXTURE_SOURCE": str(source), "REGISTERED_RECORD": str(record)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        registered = Path(record.read_text(encoding="utf-8"))
        self.assertEqual(registered, (self.dir / "skills-tree").resolve())
        self.assertTrue((registered / "scripts" / "skills_install.py").is_file())
        self.assertEqual(sorted(path.name for path in self.dir.iterdir()), ["skills-tree"])

    def test_a_failed_extract_leaves_the_kept_tree_as_it_was(self) -> None:
        self.owned_tree("skills-tree", "live")
        result = self.run_loader(self.fetch("$global:LASTEXITCODE = 2"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not extract the downloaded archive", result.stderr)
        self.assertEqual(self.content("skills-tree"), "live")
        self.assertEqual(sorted(path.name for path in self.dir.iterdir()), ["skills-tree"])

    def test_a_staging_tree_that_is_not_ours_stops_the_fetch_before_it_extracts(self) -> None:
        self.owned_tree("skills-tree", "live")
        foreign = self.dir / "skills-tree.new"
        foreign.mkdir()
        (foreign / "theirs").write_text("theirs", encoding="utf-8")
        result = self.run_loader(self.fetch("throw 'extracted over a tree that is not ours'"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("this loader did not create it", result.stderr)
        self.assertTrue((foreign / "theirs").exists())
        self.assertEqual(self.content("skills-tree"), "live")

    @unittest.skipUnless(
        sys.platform == "win32", "only Windows refuses to rename a directory holding an open file"
    )
    def test_a_held_file_in_the_live_tree_leaves_it_whole_where_the_swap_cannot_move_it(
        self,
    ) -> None:
        self.owned_tree("skills-tree.new", "new")
        self.owned_tree("skills-tree", "live")
        result = self.run_loader(f"{self.lock('skills-tree')}\nInvoke-SwapIn")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not move the previous tree", result.stderr)
        self.assertEqual(self.content("skills-tree"), "live")
        self.assertTrue((self.dir / "skills-tree" / ".bootstrap-owned").exists())
        self.assertFalse((self.dir / "skills-tree.old").exists())

    @unittest.skipUnless(
        sys.platform == "win32", "only Windows refuses to rename a directory holding an open file"
    )
    def test_a_held_file_in_the_old_tree_leaves_it_whole_where_cleanup_cannot_restore_it(
        self,
    ) -> None:
        self.owned_tree("skills-tree.old", "old")
        result = self.run_loader(f"{self.lock('skills-tree.old')}\nInvoke-Cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Could not put the previous tree back", result.stderr)
        self.assertEqual(self.content("skills-tree.old"), "old")
        self.assertTrue((self.dir / "skills-tree.old" / ".bootstrap-owned").exists())
        self.assertFalse((self.dir / "skills-tree").exists())

    def test_the_directory_lock_refuses_a_second_run_and_frees_when_the_first_ends(self) -> None:
        holder = self.hold_lock()
        result = self.run_loader("Lock-Directory")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another bootstrap run using", result.stderr)
        holder.communicate(input="\n", timeout=60)
        self.assertEqual(holder.returncode, 0, holder.stderr)
        result = self.run_loader("Lock-Directory")
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(sys.platform == "linux", "the lock bootstrap.sh takes is a Linux flock")
    def test_a_lock_bootstrap_sh_holds_refuses_this_loader(self) -> None:
        """FileShare.None is a flock on Linux, which is the one thing that makes the two loaders' locks one lock."""
        handle = hold_flock(self.dir / "skills-tree.lock")
        self.addCleanup(handle.close)
        result = self.run_loader("Lock-Directory")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another bootstrap run using", result.stderr)

    def test_an_empty_directory_at_a_managed_name_is_removed_rather_than_refused(self) -> None:
        """What a removal leaves where only the directory itself would not go, its marker already gone."""
        (self.dir / "skills-tree.new").mkdir()
        result = self.run_loader("Remove-Owned -Path (Get-StagingPath)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.dir / "skills-tree.new").exists())

    @unittest.skipUnless(sys.platform == "win32", "denies listing through a Windows ACL")
    def test_a_directory_that_cannot_be_listed_is_not_taken_for_an_empty_one(self) -> None:
        """Its contents are unknown, so ownership is refused rather than thrown out of the cleanup that asks."""
        result = self.run_loader(
            "$path = Get-Named 'skills-tree.old'\n"
            "New-Item -ItemType Directory -Path $path | Out-Null\n"
            "New-Item -ItemType File -Path (Join-Path $path 'theirs') | Out-Null\n"
            "$acl = Get-Acl -LiteralPath $path\n"
            "$rule = [Security.AccessControl.FileSystemAccessRule]::new("
            "[Security.Principal.WindowsIdentity]::GetCurrent().User, 'ListDirectory', 'Deny')\n"
            "$acl.AddAccessRule($rule)\n"
            "Set-Acl -LiteralPath $path -AclObject $acl\n"
            "try { Test-Ownership -Path $path } finally { $acl.RemoveAccessRule($rule) | Out-Null; Set-Acl -LiteralPath $path -AclObject $acl }"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_an_empty_directory_at_the_trees_name_is_replaced_by_the_new_tree(self) -> None:
        (self.dir / "skills-tree").mkdir()
        self.owned_tree("skills-tree.new", "new")
        result = self.run_loader("Invoke-SwapIn")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.content("skills-tree"), "new")
        self.assertFalse((self.dir / "skills-tree.old").exists())


STUB_TOOL = """\
#!/usr/bin/env bash
arg="${1:-}"
[[ $arg == --yes ]] && arg=""
step="$(basename "$0") $arg"
step="${step% }"
kept=absent
[[ -e "$KEPT_TREE/VERSION" ]] && kept=$(cat "$KEPT_TREE/VERSION")
printf '%s|%s|%s\\n' "$step" "$(basename "$(cd "$(dirname "$0")/../.." && pwd)")" "$kept" >>"$STEP_LOG"
[[ $step == "$FAIL_STEP" ]] && exit 7
exit 0
"""

STUB_CURL = """\
#!/usr/bin/env bash
out="" url=""
while [[ $# -gt 0 ]]; do
    case "$1" in
    -o) out="$2"; shift ;;
    -H | --retry | --connect-timeout) shift ;;
    -*) ;;
    *) url="$1" ;;
    esac
    shift
done
case "$url" in
https://api.github.com/*) printf '%s' "$STUB_COMMIT" ;;
https://codeload.github.com/*) cp "$STUB_TARBALL" "$out" ;;
*) echo "stub curl refuses $url" >&2; exit 22 ;;
esac
"""

STUB_MV = """\
#!/usr/bin/env bash
if [[ "${!#}" == "$KEPT_TREE" ]]; then
    retired=absent
    [[ -e "$KEPT_TREE.old/VERSION" ]] && retired=$(cat "$KEPT_TREE.old/VERSION")
    printf '%s\\n' "$retired" >>"$MV_LOG"
fi
exec /bin/mv "$@"
"""

STAND_UP_STEPS = (
    "install-tools.sh --sudo-timestamp",
    "upgrade-host.sh --packages",
    "install-tools.sh --install",
    "setup-github.sh --configure",
    "install-skills.sh",
)


@unittest.skipUnless(
    sys.platform == "linux" and shutil.which("flock") and shutil.which("tar"),
    "runs the Linux loader's whole main flow, which needs flock and tar",
)
class TestKeptTreeEndToEnd(unittest.TestCase):
    """`bootstrap.sh --host` run whole, with a stub curl serving a local tarball and stub tools failing at a chosen step.

    The tree the plugin loads is asserted from disk afterwards, so this holds the loader's own
    ordering to the property rather than restating it: a failed stand-up leaves the previous tree
    loading, and a successful one leaves only the new tree.
    """

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.stubs = self.root / "bin"
        self.log = self.root / "steps.log"
        self.data = self.root / "data"
        for directory in (self.home, self.stubs, self.data):
            directory.mkdir()
        curl = self.stubs / "curl"
        curl.write_text(STUB_CURL, encoding="utf-8")
        curl.chmod(0o755)
        mv = self.stubs / "mv"
        mv.write_text(STUB_MV, encoding="utf-8")
        mv.chmod(0o755)
        self.mv_log = self.root / "mv.log"
        self.tarball = self.root / "source.tar.gz"
        with tarfile.open(self.tarball, "w:gz") as archive:
            for tool in sorted({step.split()[0] for step in STAND_UP_STEPS}):
                self.add_member(
                    archive, f"ProjectTemplate-abc/host-setup/linux/{tool}", STUB_TOOL, 0o755
                )
            self.add_member(archive, "ProjectTemplate-abc/VERSION", "new", 0o644)

    @staticmethod
    def add_member(archive: tarfile.TarFile, name: str, content: str, mode: int) -> None:
        data = content.encode("utf-8")
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = mode
        archive.addfile(info, io.BytesIO(data))

    @property
    def kept(self) -> Path:
        return self.data / "skills-tree"

    def previous_tree(self) -> None:
        self.kept.mkdir()
        (self.kept / ".bootstrap-owned").touch()
        (self.kept / "VERSION").write_text("old", encoding="utf-8")

    def run_main(self, fail_step: str = "") -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.stubs}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "KEPT_TREE": str(self.kept),
            "STEP_LOG": str(self.log),
            "MV_LOG": str(self.mv_log),
            "FAIL_STEP": fail_step,
            "STUB_TARBALL": str(self.tarball),
            "STUB_COMMIT": "0123456789abcdef0123456789abcdef01234567",
        }
        return subprocess.run(
            [bash_or_skip(), str(BOOTSTRAP), "--host", "--yes", "--dir", str(self.data)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=env,
            timeout=60,
        )

    def steps(self) -> list[list[str]]:
        """Each tool run as [step, tree it ran from, version the kept tree held at that moment]."""
        if not self.log.exists():
            return []
        return [line.split("|") for line in self.log.read_text(encoding="utf-8").splitlines()]

    def version(self) -> str:
        return (self.kept / "VERSION").read_text(encoding="utf-8")

    def leftovers(self) -> list[str]:
        """Everything under the directory except the kept tree and the lock file the loader leaves by design."""
        return sorted(
            path.name
            for path in self.data.iterdir()
            if path.name not in ("skills-tree", "skills-tree.lock")
        )

    def test_a_successful_run_swaps_the_new_tree_in_and_leaves_nothing_beside_it(self) -> None:
        self.previous_tree()
        result = self.run_main()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.version(), "new")
        self.assertEqual(self.leftovers(), [], result.stderr)

    def test_the_previous_tree_is_still_held_aside_when_the_new_one_moves_into_place(self) -> None:
        self.previous_tree()
        result = self.run_main()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mv_log.read_text(encoding="utf-8").split(), ["old"], result.stderr)

    def test_a_first_run_with_no_previous_tree_installs_one(self) -> None:
        result = self.run_main()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.version(), "new")
        self.assertEqual(self.leftovers(), [], result.stderr)

    def test_the_previous_tree_is_untouched_until_the_skills_step_and_replaced_before_it_runs(
        self,
    ) -> None:
        self.previous_tree()
        result = self.run_main()
        self.assertEqual(result.returncode, 0, result.stderr)
        steps = self.steps()
        self.assertEqual([step for step, _, _ in steps], list(STAND_UP_STEPS), result.stderr)
        *before, last = steps
        for step, tree, kept in before:
            self.assertEqual(kept, "old", f"{step} found the previous tree gone or replaced")
            self.assertEqual(tree, "skills-tree.new", f"{step} ran from {tree}")
        self.assertEqual(
            last[1:], ["skills-tree", "new"], "the skills step must run from the swapped-in tree"
        )

    def test_a_stand_up_failing_before_the_skills_step_leaves_the_previous_tree_loading(
        self,
    ) -> None:
        for fail_step in STAND_UP_STEPS[:-1]:
            with self.subTest(fail_step=fail_step):
                shutil.rmtree(self.data)
                self.data.mkdir()
                self.log.unlink(missing_ok=True)
                self.previous_tree()
                result = self.run_main(fail_step)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.version(), "old", result.stderr)
                self.assertEqual(self.leftovers(), [], result.stderr)
                self.assertEqual(
                    self.steps()[-1][0], fail_step, "the run went on past the failed step"
                )

    def test_a_first_run_failing_before_the_skills_step_leaves_no_tree(self) -> None:
        result = self.run_main("install-tools.sh --install")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.kept.exists(), result.stderr)
        self.assertEqual(self.leftovers(), [], result.stderr)
        self.assertEqual(self.steps()[-1][0], "install-tools.sh --install")

    def test_a_failing_skills_installer_leaves_the_new_tree_whole_and_no_old_one_beside_it(
        self,
    ) -> None:
        self.previous_tree()
        result = self.run_main("install-skills.sh")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.steps()[-1][0], "install-skills.sh")
        self.assertEqual(self.version(), "new", result.stderr)
        self.assertEqual(self.leftovers(), [], result.stderr)


def menu_functions() -> str:
    """`menu.sh` without its closing `main "$@"`, so a test can source its functions alone."""
    lines = MENU.read_text(encoding="utf-8").rstrip("\n").split("\n")
    if lines[-1] != 'main "$@"':
        raise AssertionError(f"menu.sh no longer ends with its main call: {lines[-1]!r}")
    return "\n".join(lines[:-1]) + "\n"


@unittest.skipUnless(sys.platform == "linux", "drives the Linux menu's own functions")
class TestMenuSkillsInstall(unittest.TestCase):
    """Which installer the menu's skills task runs, since the installer registers its own directory."""

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.functions = self.dir / "functions.sh"
        self.functions.write_text(menu_functions(), encoding="utf-8")
        self.hub = self.dir / "hub"
        for relative in ("host-setup/bootstrap.sh", "host-setup/linux/install-skills.sh"):
            script = self.hub / relative
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(f'#!/bin/sh\necho "{relative} $*"\n', encoding="utf-8")
            script.chmod(0o755)

    def run_task(self, fetched: bool, extra: str = "") -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            [
                f'source "{self.functions}"',
                f'DIR="{self.dir}"',
                f'HUB_ROOT="{self.hub}"',
                f"HUB_FETCHED={'true' if fetched else 'false'}",
                extra,
                "ensure_hub_root() { return 0; }",
                "install_skills_locked",
            ]
        )
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )

    def test_a_fetched_clone_hands_the_install_to_the_bootstraps_kept_tree(self) -> None:
        """The menu removes its clone on exit, so installing from it registers a directory about to go."""
        result = self.run_task(fetched=True, extra="REF=develop\nASSUME_YES=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "host-setup/bootstrap.sh --skills --ref develop --yes"
        )

    def test_a_fetched_clone_never_passes_the_menus_cache_directory_on(self) -> None:
        """The bootstrap keeps its tree under a data directory, and the menu's own directory is a cache."""
        result = self.run_task(fetched=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--dir", result.stdout)

    def test_a_hub_checkout_the_menu_runs_from_installs_from_that_checkout(self) -> None:
        result = self.run_task(fetched=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "host-setup/linux/install-skills.sh")


class HubCleanupCases:
    """Which hub cache a menu's exit removes, the same cases for both menus.

    A session removes only the tree whose marker still records its own fetch's token, since another
    session sharing the directory may have fetched again since, and the tree is then that session's.
    """

    TOKEN = "0123456789abcdef0123456789abcdef"
    LINE_BREAK = "\n"

    dir: Path

    def run_body(self, body: str) -> subprocess.CompletedProcess[str]:
        raise NotImplementedError

    def cleanup_body(self, token: str) -> str:
        """The menu's lines setting this session's token and running its exit cleanup."""
        raise NotImplementedError

    def fetch_body(self, refetched: bool) -> str:
        """The menu's lines fetching, and fetching again as another session where asked, then exiting.

        git is stubbed to create the clone's directory, so no case reaches the network.
        """
        raise NotImplementedError

    def make_cache(self, marker: str) -> None:
        (self.dir / "hub").mkdir()
        (self.dir / "hub" / "README.md").write_text("hub\n", encoding="utf-8")
        (self.dir / "hub.owned").write_text(marker, encoding="utf-8", newline="")

    def assert_removed(self, body: str, removed: bool) -> None:
        test = cast("unittest.TestCase", self)
        result = self.run_body(body)
        test.assertEqual(result.returncode, 0, result.stderr)
        test.assertEqual((self.dir / "hub").exists(), not removed)
        test.assertEqual((self.dir / "hub.owned").exists(), not removed)

    def assert_cleaned(self, token: str, removed: bool) -> None:
        self.assert_removed(self.cleanup_body(token), removed)

    def test_a_sessions_own_fetch_is_removed_at_its_exit(self) -> None:
        """The token the fetch writes is the one cleanup reads back."""
        self.assert_removed(self.fetch_body(refetched=False), removed=True)

    def test_a_fetch_another_session_made_since_survives_the_first_sessions_exit(self) -> None:
        self.assert_removed(self.fetch_body(refetched=True), removed=False)

    def test_the_tree_this_sessions_fetch_marked_is_removed(self) -> None:
        self.make_cache(self.TOKEN)
        self.assert_cleaned(self.TOKEN, removed=True)

    def test_a_marker_ending_in_the_line_break_its_menu_writes_still_matches(self) -> None:
        self.make_cache(f"{self.TOKEN}{self.LINE_BREAK}")
        self.assert_cleaned(self.TOKEN, removed=True)

    def test_a_tree_another_session_fetched_since_is_left_in_place(self) -> None:
        self.make_cache("fedcba9876543210fedcba9876543210")
        self.assert_cleaned(self.TOKEN, removed=False)

    def test_a_marker_recording_no_token_is_left_in_place(self) -> None:
        self.make_cache("")
        self.assert_cleaned(self.TOKEN, removed=False)

    def test_a_session_with_no_token_of_its_own_removes_nothing(self) -> None:
        self.make_cache("")
        self.assert_cleaned("", removed=False)


class TestMenuHubCleanup(HubCleanupCases, unittest.TestCase):
    """`menu.sh`'s `cleanup`, with `flock` stubbed, since its lock is covered on Linux alone."""

    def setUp(self) -> None:
        self.bash = bash_or_skip()
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scripts = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.scripts / "functions.sh").write_text(menu_functions(), encoding="utf-8", newline="\n")

    def cleanup_body(self, token: str) -> str:
        return f'HUB_FETCHED=true\nHUB_FETCH_TOKEN="{token}"\ncleanup\n'

    def fetch_body(self, refetched: bool) -> str:
        again = 'mine=$HUB_FETCH_TOKEN\nfetch_hub_locked\nHUB_FETCH_TOKEN="$mine"\n'
        return (
            'git() { mkdir -p "${!#}"; }\nfetch_hub_locked\n'
            + (again if refetched else "")
            + "cleanup\n"
        )

    def test_a_token_that_cannot_be_read_fails_the_fetch_before_it_changes_anything(self) -> None:
        """errexit is suspended inside the fetch, so a failed read would otherwise mark it empty."""
        self.make_cache(self.TOKEN)
        result = self.run_body(
            'git() { mkdir -p "${!#}"; }\nod() { return 1; }\n'
            f"HUB_FETCH_TOKEN={self.TOKEN}\nrc=0\nfetch_hub_locked || rc=$?\n"
            'printf "rc=%s token=%s\\n" "$rc" "$HUB_FETCH_TOKEN"\n'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"rc=1 token={self.TOKEN}", result.stdout)
        self.assertIn("Could not read /dev/urandom", result.stderr)
        self.assertTrue((self.dir / "hub" / "README.md").exists())
        self.assertEqual((self.dir / "hub.owned").read_text(encoding="utf-8"), self.TOKEN)

    def test_a_tree_that_cannot_be_removed_fails_the_fetch_and_keeps_its_marker(self) -> None:
        """A clone into the surviving tree would fail, and its cleanup would then drop the marker."""
        self.make_cache(self.TOKEN)
        result = self.run_body(
            'git() { mkdir -p "${!#}"; }\nrm() { return 0; }\n'
            f"HUB_FETCH_TOKEN={self.TOKEN}\nrc=0\nfetch_hub_locked || rc=$?\n"
            'printf "rc=%s token=%s\\n" "$rc" "$HUB_FETCH_TOKEN"\n'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"rc=1 token={self.TOKEN}", result.stdout)
        self.assertIn("Could not remove", result.stderr)
        self.assertEqual((self.dir / "hub.owned").read_text(encoding="utf-8"), self.TOKEN)

    def test_a_marker_that_cannot_be_written_fails_the_fetch_without_cloning(self) -> None:
        """A directory at the marker's name refuses the write, which errexit would not catch here."""
        (self.dir / "hub.owned").mkdir()
        result = self.run_body(
            f'git() {{ mkdir -p "${{!#}}"; }}\nHUB_FETCH_TOKEN={self.TOKEN}\nrc=0\n'
            'fetch_hub_locked || rc=$?\nprintf "rc=%s token=%s\\n" "$rc" "$HUB_FETCH_TOKEN"\n'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"rc=1 token={self.TOKEN}", result.stdout)
        self.assertIn("Could not write", result.stderr)
        self.assertFalse((self.dir / "hub").exists())

    def run_body(self, body: str) -> subprocess.CompletedProcess[str]:
        script = self.scripts / "body.sh"
        script.write_text(
            f'source "{(self.scripts / "functions.sh").as_posix()}"\n'
            "flock() { return 0; }\n"
            f'DIR="{self.dir.as_posix()}"\n{body}',
            encoding="utf-8",
            newline="\n",
        )
        return subprocess.run(
            [self.bash, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )


MENU_PS = ROOT / "host-setup" / "menu.ps1"

POWERSHELL_MENU_LOCK_HARNESS = r"""
param([string]$Menu, [string]$Dir, [string]$BodyFile)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Menu, [ref]$tokens, [ref]$errors)
$wanted = @('info', 'fail', 'Get-HubLockPath', 'Lock-Hub', 'Invoke-WithHubLock', 'Get-MarkerPath', 'Test-HubRemovable', 'Test-HubFetchedHere', 'Invoke-FetchHubLocked', 'Invoke-Cleanup', 'Get-HubRefCommit', 'Confirm-HubRoot')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$script:DIR = $Dir
$script:HUB_LOCK = $null
$script:DRY_RUN = $false
$script:KEEP = $false
$script:HUB_FETCHED = $false
$script:HUB_FETCH_TOKEN = ''
. ([scriptblock]::Create((Get-Content -Raw -LiteralPath $BodyFile)))
"""


@unittest.skipUnless(shutil.which("pwsh"), "needs pwsh to drive the Windows menu's own functions")
class TestPowerShellMenuHubLock(unittest.TestCase):
    """The lock `menu.ps1` takes over the shared hub cache, driven through its own functions."""

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def start(self, body: str, cache: Path | None = None) -> subprocess.Popen[str]:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        harness = directory / "harness.ps1"
        harness.write_text(POWERSHELL_MENU_LOCK_HARNESS, encoding="utf-8")
        body_file = directory / "body.ps1"
        body_file.write_text(body, encoding="utf-8")
        process = subprocess.Popen(
            [
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(harness),
                "-Menu",
                str(MENU_PS),
                "-Dir",
                str(cache or self.dir),
                "-BodyFile",
                str(body_file),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.addCleanup(self._end, process)
        return process

    @staticmethod
    def _end(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=60)

    def read_line(self, process: subprocess.Popen[str]) -> str:
        """The process's next stdout line, failing the case rather than hanging where none comes."""
        output = process.stdout
        assert output is not None
        lines: list[str] = []
        reader = threading.Thread(target=lambda: lines.append(output.readline()))
        reader.start()
        reader.join(timeout=60)
        if reader.is_alive():
            process.kill()
            reader.join(timeout=10)
            self.fail("no output line within 60 seconds")
        return lines[0].strip()

    def hold(self) -> subprocess.Popen[str]:
        """A second session holding the lock through `Invoke-WithHubLock`, until its input closes."""
        holder = self.start(
            "Invoke-WithHubLock { [Console]::Out.WriteLine('held'); [void][Console]::In.ReadLine() }"
        )
        self.assertEqual(self.read_line(holder), "held")
        return holder

    def assert_waits_then_runs(self, release: Callable[[], None]) -> None:
        waiter = self.start("Invoke-WithHubLock { [Console]::Out.WriteLine('ran') }")
        self.assertIn("Waiting for another session", self.read_line(waiter))
        release()
        stdout, stderr = waiter.communicate(timeout=60)
        self.assertEqual(waiter.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "ran")

    def test_a_second_session_waits_for_the_first_and_runs_once_it_ends(self) -> None:
        holder = self.hold()

        def release() -> None:
            holder.communicate(input="\n", timeout=60)

        self.assert_waits_then_runs(release)

    def test_a_dry_run_runs_unlocked_and_creates_no_cache_directory(self) -> None:
        cache = self.dir / "absent"
        dry = self.start(
            "$script:DRY_RUN = $true\nInvoke-WithHubLock { [Console]::Out.WriteLine('ran') }", cache
        )
        stdout, stderr = dry.communicate(timeout=60)
        self.assertEqual(dry.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "ran")
        self.assertFalse(cache.exists())

    def test_a_lock_that_cannot_be_opened_returns_the_failure_value_without_running(self) -> None:
        """A directory at the lock's name is a failure to open rather than a lock to wait on."""
        (self.dir / "hub.lock").mkdir()
        failed = self.start(
            "$rc = Invoke-WithHubLock -Failed 7 { [Console]::Out.WriteLine('ran') }\n"
            '[Console]::Out.WriteLine("rc=$rc")'
        )
        stdout, stderr = failed.communicate(timeout=60)
        self.assertEqual(failed.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "rc=7")
        self.assertIn("Could not open", stderr)

    def test_a_cache_directory_that_cannot_be_created_returns_the_failure_value(self) -> None:
        """It fails the one task rather than ending the menu's whole session.

        The refusal is stubbed, since what refuses a directory differs by platform and by account.
        """
        failed = self.start(
            "function New-Item { throw 'refused' }\n"
            "$rc = Invoke-WithHubLock -Failed 7 { [Console]::Out.WriteLine('ran') }\n"
            '[Console]::Out.WriteLine("rc=$rc")',
            self.dir / "cache",
        )
        stdout, stderr = failed.communicate(timeout=60)
        self.assertEqual(failed.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "rc=7")
        self.assertIn("Could not create", stderr)

    def test_a_span_already_holding_the_lock_runs_a_nested_one_without_waiting_on_itself(
        self,
    ) -> None:
        """`Invoke-FetchHub` is reached through `Confirm-HubRoot` from inside a held span."""
        nested = self.start(
            "Invoke-WithHubLock { Invoke-WithHubLock { [Console]::Out.WriteLine('inner') } }\n"
            "Invoke-WithHubLock { [Console]::Out.WriteLine('again') }"
        )
        stdout, stderr = nested.communicate(timeout=60)
        self.assertEqual(nested.returncode, 0, stderr)
        self.assertEqual(stdout.split(), ["inner", "again"])

    @unittest.skipUnless(sys.platform == "linux", "menu.sh's lock is a Linux flock")
    def test_a_shared_lock_menu_sh_holds_makes_this_menu_wait(self) -> None:
        """A menu.sh reader holds `flock -s`, which an exclusive flock must wait out."""
        import fcntl

        handle = (self.dir / "hub.lock").open("a", encoding="utf-8")
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        self.assert_waits_then_runs(handle.close)

    @unittest.skipUnless(sys.platform == "linux", "menu.sh's lock is a Linux flock")
    def test_a_lock_this_menu_holds_refuses_menu_sh_a_shared_one(self) -> None:
        import fcntl

        holder = self.hold()
        with (self.dir / "hub.lock").open("a", encoding="utf-8") as handle:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            holder.communicate(input="\n", timeout=60)
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)


@unittest.skipUnless(shutil.which("pwsh"), "needs pwsh to drive the Windows menu's own functions")
class TestPowerShellMenuHubCleanup(HubCleanupCases, unittest.TestCase):
    """`menu.ps1`'s `Invoke-Cleanup`, under its real lock, its marker ending CRLF as on Windows."""

    LINE_BREAK = "\r\n"
    FETCH_SETUP = (
        "$script:HUB_REPO = 'owner/hub'\n$script:HUB_URL = 'https://example.invalid/hub'\n"
        "$script:DEFAULT_REF = 'main'\n$script:REF = 'main'\n$script:HUB_ROOT = ''\n"
        "function step { param([string]$Message) }\n"
        "function git { New-Item -ItemType Directory -Path $args[-1] -Force | Out-Null; "
        "$global:LASTEXITCODE = 0 }\n"
    )

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scripts = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.scripts / "harness.ps1").write_text(POWERSHELL_MENU_LOCK_HARNESS, encoding="utf-8")

    def cleanup_body(self, token: str) -> str:
        return f"$script:HUB_FETCHED = $true\n$script:HUB_FETCH_TOKEN = '{token}'\nInvoke-Cleanup\n"

    def fetch_body(self, refetched: bool) -> str:
        again = (
            "$mine = $script:HUB_FETCH_TOKEN\n"
            "[void](Invoke-FetchHubLocked)\n"
            "$script:HUB_FETCH_TOKEN = $mine\n"
        )
        return (
            self.FETCH_SETUP
            + "[void](Invoke-FetchHubLocked)\n"
            + (again if refetched else "")
            + "Invoke-Cleanup\n"
        )

    def test_a_tree_that_cannot_be_removed_fails_the_fetch_and_keeps_its_marker(self) -> None:
        """It fails the one task rather than ending the menu's whole session.

        The refusal is stubbed, since what holds a file open differs by platform.
        """
        self.make_cache(self.TOKEN)
        result = self.run_body(
            self.FETCH_SETUP
            + f"$script:HUB_FETCH_TOKEN = '{self.TOKEN}'\n"
            + "function Remove-Item { throw 'held' }\n"
            "$ok = Invoke-FetchHubLocked\n"
            '[Console]::Out.WriteLine("ok=$ok token=$script:HUB_FETCH_TOKEN")\n'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], f"ok=False token={self.TOKEN}")
        self.assertIn("Could not remove", result.stderr)
        self.assertEqual((self.dir / "hub.owned").read_text(encoding="utf-8"), self.TOKEN)

    def test_a_marker_that_cannot_be_written_fails_the_fetch_without_cloning(self) -> None:
        """It fails the one task rather than ending the menu's whole session.

        The refusal is stubbed, since what refuses a write differs by platform and by account.
        """
        result = self.run_body(
            self.FETCH_SETUP
            + f"$script:HUB_FETCH_TOKEN = '{self.TOKEN}'\n"
            + "function Set-Content { throw 'refused' }\n"
            "$ok = Invoke-FetchHubLocked\n"
            '[Console]::Out.WriteLine("ok=$ok token=$script:HUB_FETCH_TOKEN")\n'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], f"ok=False token={self.TOKEN}")
        self.assertIn("Could not write", result.stderr)
        self.assertFalse((self.dir / "hub").exists())

    def run_body(self, body: str) -> subprocess.CompletedProcess[str]:
        body_file = self.scripts / "body.ps1"
        body_file.write_text(body, encoding="utf-8")
        return subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(self.scripts / "harness.ps1"),
                "-Menu",
                str(MENU_PS),
                "-Dir",
                str(self.dir),
                "-BodyFile",
                str(body_file),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )


class HubRefFreshnessCases:
    """Whether a menu reuses the cached hub tree for its session's own ref, the same cases for both menus.

    Each run is one session sharing the cache directory, cloning from a local origin whose `main`
    and `develop` sit on different commits, reached through `insteadOf` so the menus' own hub URL
    resolves to it.
    """

    dir: Path
    work: Path
    env: dict[str, str]

    def run_body(self, body: str) -> subprocess.CompletedProcess[str]:
        raise NotImplementedError

    def fetch_body(self, ref: str) -> str:
        """The menu's lines fetching the hub at the given ref."""
        raise NotImplementedError

    def check_body(self, ref: str) -> str:
        """The menu's lines confirming the cached tree for the given ref, printing which way it went.

        `fresh` where the tree is reused, and `refetched` where the menu would fetch again.
        """
        raise NotImplementedError

    def make_origin(self, root: Path) -> None:
        self.work = root / "work"
        origin = root / "origin.git"
        self.git("init", "--quiet", "--initial-branch=main", str(self.work), cwd=root)
        self.commit("main")
        self.git("checkout", "--quiet", "-b", "develop")
        self.commit("develop")
        self.git("clone", "--quiet", "--bare", str(self.work), str(origin), cwd=root)
        self.git("remote", "add", "origin", str(origin))
        self.env = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{origin.as_posix()}.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/ptr727/ProjectTemplate",
        }

    def git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(
            ["git", *args],
            cwd=cwd or self.work,
            check=True,
            capture_output=True,
            timeout=60,
        )

    def commit(self, message: str) -> None:
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            message,
        )

    def assert_check(self, ref: str, expected: str) -> None:
        test = cast("unittest.TestCase", self)
        result = self.run_body(self.check_body(ref))
        test.assertEqual(result.returncode, 0, result.stderr)
        test.assertEqual(result.stdout.strip().splitlines()[-1:], [expected], result.stderr)

    def fetch(self, ref: str) -> None:
        test = cast("unittest.TestCase", self)
        result = self.run_body(self.fetch_body(ref))
        test.assertEqual(result.returncode, 0, result.stderr)

    def test_a_tree_fetched_at_main_is_reused_by_a_main_session(self) -> None:
        self.fetch("main")
        self.assert_check("main", "fresh")

    def test_a_tree_fetched_at_another_ref_is_reused_by_a_session_on_that_ref(self) -> None:
        """Compared against main alone, it read as stale and was cloned again on every task."""
        self.fetch("develop")
        self.assert_check("develop", "fresh")

    def test_a_tree_another_session_replaced_with_main_is_stale_for_another_ref(self) -> None:
        """Compared against main alone, the session ran its next task from main without saying so."""
        self.fetch("develop")
        self.fetch("main")
        self.assert_check("develop", "refetched")

    def test_a_tree_fetched_at_another_ref_is_stale_for_a_main_session(self) -> None:
        self.fetch("develop")
        self.assert_check("main", "refetched")

    def test_a_tree_whose_ref_moved_on_origin_since_its_fetch_is_stale(self) -> None:
        self.fetch("develop")
        self.commit("develop again")
        self.git("push", "--quiet", "origin", "develop")
        self.assert_check("develop", "refetched")


class TestMenuHubRefFreshness(HubRefFreshnessCases, unittest.TestCase):
    """`menu.sh`'s `ensure_hub_root`, with `flock` stubbed, since its lock is covered on Linux alone."""

    def setUp(self) -> None:
        self.bash = bash_or_skip()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.dir = root / "cache"
        self.dir.mkdir()
        self.scripts = root / "scripts"
        self.scripts.mkdir()
        (self.scripts / "functions.sh").write_text(menu_functions(), encoding="utf-8", newline="\n")
        self.make_origin(root)

    def fetch_body(self, ref: str) -> str:
        return f"REF={ref}\nfetch_hub_locked\n"

    def check_body(self, ref: str) -> str:
        return (
            f'REF={ref}\nHUB_ROOT="$DIR/hub"\n'
            "fetch_hub() { echo refetched; return 1; }\n"
            "if ensure_hub_root; then echo fresh; fi\n"
        )

    def run_body(self, body: str) -> subprocess.CompletedProcess[str]:
        script = self.scripts / "body.sh"
        script.write_text(
            f'source "{(self.scripts / "functions.sh").as_posix()}"\n'
            "flock() { return 0; }\n"
            f'DIR="{self.dir.as_posix()}"\n{body}',
            encoding="utf-8",
            newline="\n",
        )
        return subprocess.run(
            [self.bash, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=self.env,
            timeout=60,
        )


@unittest.skipUnless(shutil.which("pwsh"), "needs pwsh to drive the Windows menu's own functions")
class TestPowerShellMenuHubRefFreshness(HubRefFreshnessCases, unittest.TestCase):
    """`menu.ps1`'s `Confirm-HubRoot`, driven through its own functions."""

    SETUP = (
        "$script:HUB_REPO = 'ptr727/ProjectTemplate'\n"
        "$script:HUB_URL = 'https://github.com/ptr727/ProjectTemplate'\n"
        "$script:DEFAULT_REF = 'main'\n$script:HUB_ROOT = ''\n"
        "function step { param([string]$Message) }\n"
    )

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.dir = root / "cache"
        self.dir.mkdir()
        self.scripts = root / "scripts"
        self.scripts.mkdir()
        (self.scripts / "harness.ps1").write_text(POWERSHELL_MENU_LOCK_HARNESS, encoding="utf-8")
        self.make_origin(root)

    def fetch_body(self, ref: str) -> str:
        return (
            self.SETUP
            + f"$script:REF = '{ref}'\n"
            + "if (-not (Invoke-FetchHubLocked)) { exit 1 }\n"
        )

    def check_body(self, ref: str) -> str:
        return (
            self.SETUP + f"$script:REF = '{ref}'\n$script:HUB_ROOT = Join-Path $script:DIR 'hub'\n"
            "function Invoke-FetchHub { [Console]::Out.WriteLine('refetched'); return $false }\n"
            "if (Confirm-HubRoot) { [Console]::Out.WriteLine('fresh') }\n"
        )

    def run_body(self, body: str) -> subprocess.CompletedProcess[str]:
        body_file = self.scripts / "body.ps1"
        body_file.write_text(body, encoding="utf-8")
        return subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(self.scripts / "harness.ps1"),
                "-Menu",
                str(MENU_PS),
                "-Dir",
                str(self.dir),
                "-BodyFile",
                str(body_file),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=self.env,
            timeout=60,
        )


class TestHarness(unittest.TestCase):
    def test_this_module_collects_a_plausible_number_of_cases(self) -> None:
        """A module whose cases fail to load still reports OK, which is a pass proving nothing."""
        loaded = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertGreaterEqual(loaded.countTestCases(), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)

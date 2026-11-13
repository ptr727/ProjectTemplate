#!/usr/bin/env python3
"""Tests for the host tool installers' JSON report, on both platforms.

The JSON report exists so a program can read what the table tells a person.
Its contract is therefore the shape a program parses:

- every field present on every row
- a version that was not read is written as null
- each note filed under the tool that raised it

Each installer's report is driven through its own functions with the host reads stubbed.
This is because a real report reads the host it runs on.

Run as `python3 tests/test_install_tools.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from sdk_baseline import baseline_line

ROOT = Path(__file__).resolve().parent.parent
LINUX_INSTALLER = ROOT / "host-setup" / "linux" / "install-tools.sh"
WINDOWS_INSTALLER = ROOT / "host-setup" / "windows" / "install-tools.ps1"
WINDOWS_UPGRADER = ROOT / "host-setup" / "windows" / "upgrade-host.ps1"

AWKWARD = 'a "quoted" back\\slash\ttab\nnewline\rreturn \x01\x1f \u00e9 end'


def linux_functions() -> str:
    """`install-tools.sh` without its closing `main "$@"`, so a test can source its functions alone."""
    lines = LINUX_INSTALLER.read_text(encoding="utf-8").rstrip("\n").split("\n")
    if lines[-1] != 'main "$@"':
        raise AssertionError(f"install-tools.sh no longer ends with its main call: {lines[-1]!r}")
    return "\n".join(lines[:-1]) + "\n"


class LinuxFunctionsCase(unittest.TestCase):
    """A case that sources the Linux installer's functions into a fresh bash for each body it runs."""

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.functions = self.dir / "functions.sh"
        self.functions.write_text(linux_functions(), encoding="utf-8")

    def run_bash(self, body: str, argument: str = "") -> subprocess.CompletedProcess[str]:
        script = f'source "{self.functions}"\n{body}\n'
        return subprocess.run(
            ["bash", "-c", script, "bash", argument],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )


@unittest.skipUnless(sys.platform == "linux", "drives the Linux installer's own functions")
class TestLinuxJsonReport(LinuxFunctionsCase):
    """`install-tools.sh --json`, driven through the script's own functions."""

    def test_json_string_round_trips_every_escaped_character(self) -> None:
        result = self.run_bash('json_string "$1"', AWKWARD)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), AWKWARD)

    def test_report_writes_each_row_and_files_each_note(self) -> None:
        body = f"""
host_path=$PATH
PATH="{self.dir}/empty"
JSON_OUTPUT=true
SELECTED=(jq extra)
REPO_PACKAGES[extra]=extra-package
jq_version() {{ :; }}
jq_target() {{ printf '1.8.2'; }}
apt_installed_version() {{ printf '2.0'; }}
apt_candidate_version() {{ printf '2.0'; }}
tool_note() {{ note "$1" "$AWKWARD"; }}
AWKWARD=$1
report
PATH=$host_path
"""
        result = self.run_bash(body, AWKWARD)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["platform"], "linux")
        self.assertEqual(
            report["tools"],
            [
                {
                    "tool": "jq",
                    "installed": None,
                    "available": "1.8.2",
                    "source": "jqlang/jq",
                    "mechanism": "binary",
                    "status": "missing",
                    "notes": [AWKWARD],
                },
                {
                    "tool": "extra",
                    "installed": "2.0",
                    "available": "2.0",
                    "source": "apt:extra-package",
                    "mechanism": "apt",
                    "status": "current",
                    "notes": [],
                },
            ],
        )
        self.assertEqual(len(report["notes"]), 1)
        self.assertIn("curl is not installed", report["notes"][0])

    def test_bin_dir_joins_a_path_that_leaves_it_out(self) -> None:
        result = self.run_bash('PATH=/usr/bin:/bin\nensure_bin_dir_on_path\nprintf "%s" "$PATH"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/usr/local/bin:/usr/bin:/bin")

    def test_bin_dir_joins_an_empty_path_without_a_trailing_separator(self) -> None:
        result = self.run_bash(
            'host_path=$PATH\nPATH=""\nensure_bin_dir_on_path\nresult=$PATH\nPATH=$host_path\nprintf "%s" "$result"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/usr/local/bin")

    def test_a_path_naming_bin_dir_keeps_its_order_and_its_real_shadow(self) -> None:
        bin_dir = self.dir / "shadow"
        bin_dir.mkdir()
        tool = bin_dir / "sometool"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
        body = f"""PATH="{bin_dir}:/usr/bin:/usr/local/bin/:/bin"
before=$PATH
ensure_bin_dir_on_path
[[ $PATH == "$before" ]] || {{ echo "changed: $PATH"; exit 1; }}
tool_shadow_path sometool"""
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{bin_dir}/sometool")

    def test_bin_dir_spelled_differently_is_still_on_the_path(self) -> None:
        body = 'PATH="/usr//local/./bin:/usr/bin"\nbefore=$PATH\nensure_bin_dir_on_path\n[[ $PATH == "$before" ]] && printf same'
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "same")

    def test_main_puts_bin_dir_on_the_path_before_reporting(self) -> None:
        body = """parse_args() { :; }
load_repo_tools() { :; }
resolve_selection() { :; }
detect_host() { :; }
report() { printf '%s' "$PATH"; }
host_path=$PATH
PATH=/usr/bin:/bin
main"""
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/usr/local/bin:/usr/bin:/bin")

    def test_minimal_path_reports_no_shadow_for_a_distro_copy(self) -> None:
        distro = self.dir / "distro"
        distro.mkdir()
        tool = distro / "sometool"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
        body = (
            f'host_path=$PATH\nPATH="{distro}"\nensure_bin_dir_on_path\n'
            'result=$(tool_shadow_path sometool)\nPATH=$host_path\nprintf "%s" "$result"'
        )
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_json_string_drops_bytes_that_are_not_utf8(self) -> None:
        never_valid = b"\xff"
        above_the_last_code_point = b"\xf4\x90\x80\x80"
        overlong = b"\xe0\x80\x80"
        surrogate = b"\xed\xa0\x80"
        truncated = b"\xe2\x82"
        lead_before_ascii = b"\xc3"
        malformed = (
            b"a"
            + never_valid
            + above_the_last_code_point
            + overlong
            + surrogate
            + lead_before_ascii
            + "b\u00e9\U0001f600".encode()
            + truncated
        )
        for locale in ("C", "C.UTF-8"):
            with self.subTest(locale=locale):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        f'source "{self.functions}"\njson_string "$1"',
                        "bash",
                        malformed,
                    ],
                    capture_output=True,
                    check=False,
                    timeout=30,
                    env={"PATH": os.environ["PATH"], "LC_ALL": locale},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                self.assertTrue(result.stdout.isascii(), result.stdout)
                self.assertEqual(json.loads(result.stdout), "ab\u00e9\U0001f600")

    def test_docker_inside_wsl_names_docker_desktop_as_its_mechanism(self) -> None:
        for is_wsl, expected in (("true", "docker-desktop"), ("false", "apt")):
            with self.subTest(is_wsl=is_wsl):
                result = self.run_bash(f"IS_WSL={is_wsl}\ntool_mechanism docker")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_selection_naming_a_tool_other_than_the_last_succeeds(self) -> None:
        result = self.run_bash('REQUESTED=(jq)\nresolve_selection\nprintf "%s\\n" "${SELECTED[@]}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "jq\n")

    def test_note_paths_under_home_are_written_with_a_tilde(self) -> None:
        body = """
HOME=/srv/example-user
JSON_OUTPUT=true
SELECTED=(jq)
tool_shadow_path() { printf '/srv/example-user/.local/bin/%s' "$1"; }
jq_version() { :; }
jq_target() { :; }
report
"""
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("/srv/example-user", result.stdout)
        notes = json.loads(result.stdout)["tools"][0]["notes"]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("~/.local/bin/jq "), notes[0])

    def test_hide_home_only_rewrites_a_whole_leading_home_component(self) -> None:
        cases = {
            "/srv/example-user": "~",
            "/srv/example-user/bin": "~/bin",
            "/srv/example-user2/bin": "/srv/example-user2/bin",
            "/usr/local/bin": "/usr/local/bin",
            "": "",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                result = self.run_bash('HOME=/srv/example-user\nhide_home "$1"', path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_report_with_no_rows_is_still_an_object(self) -> None:
        result = self.run_bash("JSON_OUTPUT=true\nSELECTED=()\nreport")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["tools"], [])

    def test_json_is_refused_beside_another_action(self) -> None:
        for action in ("--install", "--upgrade", "--list", "--sudo-timestamp"):
            with self.subTest(action=action):
                result = subprocess.run(
                    [str(LINUX_INSTALLER), action, "--json"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("--json", result.stderr)
                self.assertEqual(result.stdout, "")


DPKG_QUERY_STUB = r"""#!/bin/sh
format=$2
line=$(grep "^$3 " "$DPKG_TABLE") || exit 1
set -- $line
case $format in
*Status-Want*) printf '%s' "$2" ;;
*Status-Status*) printf '%s' "$3" ;;
*) printf '%s ok %s' "$2" "$3" ;;
esac
"""


@unittest.skipUnless(sys.platform == "linux", "drives the Linux installer's own functions")
class TestLinuxHeldPackages(LinuxFunctionsCase):
    """A held package reads as installed, and a held Docker conflict is reported rather than removed.

    The stub dpkg-query answers each status field a format names from a table of `package want status` lines.
    A definition reading the wrong field therefore reads the wrong answer.
    """

    def setUp(self) -> None:
        super().setUp()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        stub = self.bin / "dpkg-query"
        stub.write_text(DPKG_QUERY_STUB, encoding="ascii")
        stub.chmod(0o755)
        self.table = self.dir / "dpkg-table"

    def run_with_table(self, rows: str, body: str) -> subprocess.CompletedProcess[str]:
        self.table.write_text(rows, encoding="ascii")
        script = f'source "{self.functions}"\nPATH="{self.bin}:$PATH"\n{body}\n'
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
            env={**os.environ, "DPKG_TABLE": str(self.table)},
        )

    def docker_conflicts(self, rows: str) -> subprocess.CompletedProcess[str]:
        """Run docker_install up to the point past its conflict removal, recording each root command."""
        body = """IS_WSL=false
CODENAME=trixie
run_root() { printf 'ROOT %s\\n' "$*"; }
ensure_prerequisites() { printf 'PAST REMOVAL\\n'; exit 0; }
if docker_install; then printf 'RC 0\\n'; else printf 'RC %s\\n' "$?"; fi"""
        return self.run_with_table(rows, body)

    def test_a_held_package_reads_as_installed_and_is_not_planned(self) -> None:
        rows = "less hold installed\njq install installed\ngone deinstall config-files\n"
        result = self.run_with_table(rows, "apt_missing less jq gone absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["gone", "absent"])

    def test_a_held_conflict_is_reported_and_nothing_is_removed(self) -> None:
        result = self.docker_conflicts("runc hold installed\ndocker.io install installed\n")
        self.assertEqual(result.stdout.splitlines(), ["RC 1"])
        self.assertIn("runc are held", result.stderr)
        self.assertIn("apt-mark unhold runc", result.stderr)

    def test_an_unheld_conflict_is_removed(self) -> None:
        result = self.docker_conflicts("docker.io install installed\nrunc install installed\n")
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "  Removing 2 conflicting package(s): docker.io runc",
                "ROOT apt-get remove -y docker.io runc",
                "PAST REMOVAL",
            ],
        )


@unittest.skipUnless(sys.platform == "linux", "drives the Linux installer's own functions")
class TestLinuxOptionalTools(LinuxFunctionsCase):
    """An optional tool joins the default selection only under --optional, and naming it selects it."""

    def selection(self, body: str) -> list[str]:
        result = self.run_bash(f'{body}\nresolve_selection\nprintf "%s\\n" "${{SELECTED[@]}}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_the_default_selection_leaves_every_optional_tool_out(self) -> None:
        selected = self.selection("")
        self.assertNotIn("powershell", selected)
        self.assertNotIn("op", selected)
        self.assertIn("gh", selected)
        self.assertIn("dotnet", selected)

    def test_optional_brings_every_optional_tool_in(self) -> None:
        selected = self.selection("WITH_OPTIONAL=true")
        self.assertIn("powershell", selected)
        self.assertIn("op", selected)

    def test_naming_an_optional_tool_selects_it_without_the_flag(self) -> None:
        self.assertEqual(self.selection("REQUESTED=(op)"), ["op"])

    def test_the_list_marks_each_optional_tool_and_only_those(self) -> None:
        result = self.run_bash("list_tools")
        self.assertEqual(result.returncode, 0, result.stderr)
        marked = [line.split()[0] for line in result.stdout.splitlines() if "(optional)" in line]
        self.assertEqual(marked, ["powershell", "op"])


@unittest.skipUnless(sys.platform == "linux", "drives the Linux installer's own functions")
class TestLinuxOpInstall(LinuxFunctionsCase):
    """`op` comes from 1Password's own apt repository, and its manual download goes once apt has it."""

    RECORDING = """
ensure_prerequisites() { :; }
remove_stale() { printf 'STALE %s\\n' "$*"; }
install_keyring() { printf 'KEYRING %s\\n' "$*"; return 1; }
write_sources() { printf 'SOURCES %s\\n' "$*"; return 1; }
apt_install() { printf 'APT %s\\n' "$*"; }
op_remove_download() { printf 'REMOVE\\n'; }
op_version() { :; }
"""

    def unowned_copy(self) -> Path:
        """A directory holding an executable `op`, standing in for a download on PATH."""
        directory = self.dir / "downloaded"
        directory.mkdir()
        binary = directory / "op"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        return directory

    def removal(
        self, owned: int, only: str, setup: str = "DRY_RUN=true"
    ) -> subprocess.CompletedProcess[str]:
        """Run the shared removal over an `op` first on PATH, as apply_tool runs an install, with errexit off."""
        directory = self.unowned_copy()
        body = (
            f'PATH="{directory}:$PATH"\n'
            f"dpkg_owns_path() {{ return {owned}; }}\n"
            f"{setup}\n"
            f'if remove_unowned_command op op "1Password CLI" "{only}"; then exit 0; else exit "$?"; fi'
        )
        return self.run_bash(body)

    def test_install_registers_the_repository_for_the_host_architecture(self) -> None:
        result = self.run_bash(f"{self.RECORDING}\nARCH=arm64\nop_install")
        self.assertEqual(result.returncode, 0, result.stderr)
        site = "https://downloads.1password.com/linux"
        keyring = "/etc/apt/keyrings/1password-archive-keyring.gpg"
        vendor_keyring = "/usr/share/keyrings/1password-archive-keyring.gpg"
        self.assertEqual(
            result.stdout.splitlines(),
            [
                f"KEYRING {site}/keys/1password.asc {keyring} {site}/debian/arm64/dists/stable/InRelease true",
                f"SOURCES 1password {site}/debian/arm64 stable main {keyring}",
                f"STALE /etc/apt/sources.list.d/1password.list {vendor_keyring}",
                "APT 1password-cli",
                "REMOVE",
            ],
        )

    def test_a_failed_install_keeps_the_downloaded_copy(self) -> None:
        body = f"""{self.RECORDING}
apt_install() {{ printf 'APT %s\\n' "$*"; return 100; }}
ARCH=amd64
if op_install; then exit 0; else exit "$?"; fi"""
        result = self.run_bash(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("REMOVE", result.stdout)

    def test_install_beside_an_existing_package_keeps_the_download(self) -> None:
        for mode, removed in (("install", False), ("upgrade", True)):
            with self.subTest(mode=mode):
                body = f"""{self.RECORDING}
op_version() {{ printf '2.41.0-1'; }}
MODE={mode}
ARCH=amd64
op_install"""
                result = self.run_bash(body)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("REMOVE" in result.stdout.splitlines(), removed, result.stdout)

    def test_an_architecture_without_a_build_is_skipped_and_changes_nothing(self) -> None:
        result = self.run_bash(f"{self.RECORDING}\nARCH=i386\nop_install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("skipping op on i386", result.stderr)

    def test_an_unowned_copy_at_the_one_named_path_is_removed(self) -> None:
        result = self.removal(1, str(self.dir / "downloaded" / "op"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"[dry run] rm -f {self.dir}/downloaded/op", result.stdout)

    def test_an_unowned_copy_anywhere_else_is_left_with_a_warning(self) -> None:
        result = self.removal(1, "/usr/local/bin/op")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("rm -f", result.stdout)
        self.assertIn("so it stays", result.stderr)

    def test_a_copy_dpkg_owns_is_left_alone(self) -> None:
        result = self.removal(0, "")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_failed_ownership_query_stops_rather_than_removing(self) -> None:
        result = self.removal(2, "")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("rm -f", result.stdout)
        self.assertIn("Cannot determine dpkg ownership", result.stderr)

    def test_a_failed_removal_warns_once_rather_than_looping(self) -> None:
        result = self.removal(1, "", "ASSUME_YES=true\nrun_root() { return 1; }")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("failed to remove"), 1, result.stderr)

    def test_a_failed_ownership_query_stops_the_shadow_removal_too(self) -> None:
        directory = self.unowned_copy()
        (directory / "op").rename(directory / "jq")
        body = f"""PATH="{directory}:$PATH"
dpkg_owns_path() {{ return 2; }}
DRY_RUN=true
if tool_unshadow jq; then exit 0; else exit "$?"; fi"""
        result = self.run_bash(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("rm -f", result.stdout)
        self.assertIn("Cannot determine dpkg ownership", result.stderr)

    def test_unshadowing_waits_for_the_package(self) -> None:
        body = """op_version() { :; }
op_remove_download() { printf 'REMOVE\\n'; }
tool_unshadow op"""
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_upgrade_unshadows_beside_a_package_that_reads_current(self) -> None:
        body = """MODE=upgrade
op_version() { printf '2.42.0-1'; }
op_target() { printf '2.42.0-1'; }
tool_configured() { return 0; }
op_remove_download() { printf 'UNSHADOW\\n'; }
op_install() { printf 'INSTALL\\n'; }
ARCH=amd64
apply_tool op"""
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(), ["UNSHADOW", "op: current at 2.42.0-1, leaving it alone"]
        )

    def test_a_hand_installed_package_reads_unmanaged_until_the_repository_is_configured(
        self,
    ) -> None:
        body = """tool_configured() { return 1; }
tool_effective_status op 2.42.0-1 2.42.0-1"""
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "unmanaged")

    def test_the_report_names_an_unconfigured_repository_and_an_unowned_copy(self) -> None:
        cases = {
            "/usr/local/bin/op": "is an unowned downloaded copy, which an install that adds",
            "/opt/example/bin/op": "belongs to no package, and an install or upgrade leaves it",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                body = f"""tool_configured() {{ return 1; }}
unowned_command_path() {{ printf '{path}'; }}
ARCH=amd64
tool_note op
printf '%s\\n' "${{NOTES[@]}}" """
                result = self.run_bash(body)
                self.assertEqual(result.returncode, 0, result.stderr)
                notes = result.stdout.splitlines()
                self.assertEqual(len(notes), 2, notes)
                self.assertIn("1Password's repository is not configured", notes[0])
                self.assertIn(f"{path} {expected}", notes[1])

    def test_the_configured_repository_is_the_one_install_writes(self) -> None:
        text = LINUX_INSTALLER.read_text(encoding="utf-8")
        self.assertRegex(text, r'(?m)^    op\) \[\[ -f "\$SOURCES_DIR/1password\.sources" \]\] ;;$')
        self.assertRegex(text, r'write_sources "1password" ')


class TestInstallersNameTheBaselineLine(unittest.TestCase):
    """Each installer names the SDK line the global.json baseline selects.

    The installers name it rather than read the baseline, so each still runs fetched on its own.
    This is what makes a TFM move edit all three together.
    """

    def test_the_linux_line_is_the_baseline_line(self) -> None:
        major, minor = baseline_line()
        text = LINUX_INSTALLER.read_text(encoding="utf-8")
        self.assertRegex(text, rf'(?m)^readonly DOTNET_FLEET_LINE="{major}\.{minor}"$')

    def test_the_windows_major_is_the_baseline_major(self) -> None:
        major, _ = baseline_line()
        text = WINDOWS_INSTALLER.read_text(encoding="utf-8")
        self.assertRegex(text, rf"(?m)^\$DOTNET_MAJOR = {major}$")


@unittest.skipUnless(sys.platform == "linux", "drives the Linux installer's own functions")
class TestLinuxDotnetSets(LinuxFunctionsCase):
    """The dotnet sets carry the fleet line beside the newest line, and the report reads the fleet line."""

    def setUp(self) -> None:
        super().setUp()
        major, minor = baseline_line()
        self.fleet = f"dotnet-sdk-{major}.{minor}"
        self.older = f"dotnet-sdk-{major - 1}.0"
        self.newer = f"dotnet-sdk-{major + 1}.0"

    def install(
        self,
        feed: list[str],
        optional: bool = False,
        apt_install: str = 'echo "INSTALL $*"',
        source: str = "distro",
    ) -> subprocess.CompletedProcess[str]:
        """Run `dotnet_install` with `feed` as the packages apt sees and `apt_install` as its body.

        It runs inside an `if`, as apply_tool runs it, where set -e is off.
        So the run's exit status is the function's own return, not an errexit stop at a failed call.
        """
        return self.run_bash(f"""
apt_refresh() {{ :; }}
dotnet_feed() {{ return 0; }}
dotnet_source() {{ printf '%s' "{source}"; }}
apt_install() {{ {apt_install}; }}
dotnet_sdk_packages() {{ printf '%s\\n' {" ".join(feed)}; }}
WITH_OPTIONAL={"true" if optional else "false"}
DISTRO_ID=debian
DISTRO_VERSION=13
if dotnet_install; then exit 0; else exit "$?"; fi
""")

    def test_a_newest_line_install_is_recorded_as_a_change(self) -> None:
        """The row reads the fleet line, so the newest line's own move is what the summary needs."""
        result = self.run_bash(f"""
apt_refresh() {{ :; }}
dotnet_feed() {{ return 0; }}
INSTALLED=""
apt_install() {{ INSTALLED="$*"; }}
apt_installed_version() {{ [[ " $INSTALLED " == *" $1 "* ]] && printf '1.0'; return 0; }}
dotnet_sdk_packages() {{ printf '%s\\n' {self.fleet} {self.newer}; }}
WITH_OPTIONAL=false
DISTRO_ID=debian
DISTRO_VERSION=13
CHANGED=()
if dotnet_install; then printf '%s\\n' "${{CHANGED[@]}}"; else exit "$?"; fi
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"{self.newer} - -> 1.0"])

    def test_without_a_fleet_line_the_newest_line_is_recorded_once(self) -> None:
        """The row reads the newest line then, so apply_tool records it and dotnet_install does not."""
        result = self.run_bash(f"""
apt_refresh() {{ :; }}
dotnet_feed() {{ return 0; }}
INSTALLED=""
apt_install() {{ INSTALLED="$*"; }}
apt_installed_version() {{ [[ " $INSTALLED " == *" $1 "* ]] && printf '1.0'; return 0; }}
dotnet_sdk_packages() {{ printf '%s\\n' {self.newer}; }}
WITH_OPTIONAL=false
DISTRO_ID=debian
DISTRO_VERSION=13
CHANGED=()
if dotnet_install; then echo "STATUS 0"; else echo "STATUS $?"; fi
[[ ${{#CHANGED[@]}} -eq 0 ]] || printf '%s\\n' "${{CHANGED[@]}}"
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["STATUS 0"])

    def test_a_failed_optional_install_still_records_the_default_move(self) -> None:
        result = self.run_bash(f"""
apt_refresh() {{ :; }}
dotnet_feed() {{ return 0; }}
INSTALLED=""
apt_install() {{ [[ " $* " != *" {self.older} "* ]] || return 1; INSTALLED="$INSTALLED $*"; }}
apt_installed_version() {{ [[ " $INSTALLED " == *" $1 "* ]] && printf '1.0'; return 0; }}
dotnet_sdk_packages() {{ printf '%s\\n' {self.older} {self.fleet} {self.newer}; }}
WITH_OPTIONAL=true
DISTRO_ID=debian
DISTRO_VERSION=13
CHANGED=()
if dotnet_install; then echo "STATUS 0"; else echo "STATUS $?"; fi
printf '%s\\n' "${{CHANGED[@]}}"
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["STATUS 1", f"{self.newer} - -> 1.0", "dotnet - -> 1.0"],
        )

    def test_an_empty_feed_fails_the_tool_and_lets_the_run_carry_on(self) -> None:
        result = self.install([])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("No .NET SDK package is available for debian 13", result.stderr)

    def test_the_fleet_line_installs_beside_a_newer_one(self) -> None:
        result = self.install([self.older, self.fleet, self.newer])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"INSTALL {self.fleet} {self.newer}"])

    def test_the_optional_set_skips_both_default_lines(self) -> None:
        result = self.install([self.older, self.fleet, self.newer], optional=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [f"INSTALL {self.fleet} {self.newer}", f"INSTALL {self.older}"],
        )

    def test_a_failed_default_install_fails_the_tool(self) -> None:
        """apply_tool runs the install with set -e off, so the status has to be the function's own."""
        result = self.install(
            [self.older, self.fleet, self.newer],
            optional=True,
            apt_install=f'echo "INSTALL $*"; [[ " $* " != *" {self.fleet} "* ]]',
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"INSTALL {self.fleet} {self.newer}"])

    def test_a_failed_optional_install_fails_the_tool(self) -> None:
        result = self.install(
            [self.older, self.fleet, self.newer],
            optional=True,
            apt_install=f'echo "INSTALL $*"; [[ " $* " != *" {self.older} "* ]]',
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [f"INSTALL {self.fleet} {self.newer}", f"INSTALL {self.older}"],
        )

    def test_a_fleet_line_that_is_the_newest_installs_once(self) -> None:
        result = self.install([self.older, self.fleet])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"INSTALL {self.fleet}"])

    def test_a_distro_feed_without_the_fleet_line_installs_the_newest_and_carries_on(self) -> None:
        result = self.install([self.newer])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"INSTALL {self.newer}"])
        self.assertIn(f"The distro feed carries no {self.fleet}", result.stderr)
        self.assertIn("is the mixing that breaks a host", result.stderr)

    def test_microsoft_s_feed_without_the_fleet_line_is_named_as_such(self) -> None:
        result = self.install([self.newer], source="packages.microsoft.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Microsoft's feed carries no {self.fleet} for debian 13", result.stderr)
        self.assertNotIn("mixing", result.stderr)

    def report(self, feed: list[str], pending: str = "", versions: str = "1.0 1.1") -> list[str]:
        """The dotnet row's installed and target versions, then each note `tool_note` files for it.

        Each package's version is its own name, so a row shows which package it read. The `pending`
        package alone carries real versions instead, installed then candidate from `versions`, so
        `tool_status` judges them by dpkg's ordering.
        """
        have, want = versions.split()
        result = self.run_bash(f"""
dotnet_sdk_packages() {{ printf '%s\\n' {" ".join(feed)}; }}
apt_installed_version() {{ if [[ $1 == "{pending}" ]]; then printf '{have}'; else printf '%s' "$1"; fi; }}
apt_candidate_version() {{ if [[ $1 == "{pending}" ]]; then printf '{want}'; else printf '%s' "$1"; fi; }}
apt_missing() {{ :; }}
NOTES=()
NOTE_TEXTS=()
dotnet_version
echo
dotnet_target
echo
tool_note dotnet
[[ ${{#NOTES[@]}} -eq 0 ]] || printf '%s\\n' "${{NOTES[@]}}"
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_the_row_reports_the_fleet_line_over_a_newer_one(self) -> None:
        self.assertEqual(self.report([self.fleet, self.newer]), [self.fleet, self.fleet])

    @unittest.skipUnless(shutil.which("dpkg"), "tool_status orders versions with dpkg")
    def test_a_pending_newest_line_upgrade_is_noted(self) -> None:
        self.assertEqual(
            self.report([self.fleet, self.newer], pending=self.newer),
            [
                self.fleet,
                self.fleet,
                f"dotnet: {self.newer} is at 1.0 and the feed carries 1.1, --upgrade moves it",
            ],
        )

    @unittest.skipUnless(shutil.which("dpkg"), "tool_status orders versions with dpkg")
    def test_a_newest_line_ahead_of_its_candidate_is_not_noted(self) -> None:
        """apt never downgrades, so an installed version past the candidate has nothing to move."""
        self.assertEqual(
            self.report([self.fleet, self.newer], pending=self.newer, versions="1.2 1.1"),
            [self.fleet, self.fleet],
        )

    def test_without_a_fleet_line_a_pending_upgrade_is_the_row_s_alone(self) -> None:
        self.assertEqual(
            self.report([self.newer], pending=self.newer),
            [
                "1.0",
                "1.1",
                f"dotnet: the feed carries no {self.fleet}, the line fleet .NET repositories select",
            ],
        )

    def test_a_feed_without_the_fleet_line_is_noted_and_the_newest_reported(self) -> None:
        self.assertEqual(
            self.report([self.newer]),
            [
                self.newer,
                self.newer,
                f"dotnet: the feed carries no {self.fleet}, the line fleet .NET repositories select",
            ],
        )


WINDOWS_HARNESS = r"""
param([string]$Installer, [string]$Awkward, [string]$Mode)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('note', 'Get-NoteText', 'Show-Report', 'Resolve-Mode', 'die', 'Add-PathLengthNote', 'Get-PathLengthWarning')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
function log { param([string]$Message = '') Write-Host $Message }
function info { param([string]$Message) Write-Host "  $Message" }
function Get-HostPathLength { 0 }
function Get-Tool { param([string]$Name) @{ Name = $Name } }
function Get-ToolState {
    param([hashtable]$Tool)
    if ($Tool.Name -eq 'jq') {
        return @{ Package = 'jqlang.jq'; Installed = $null; Rows = @(); Available = '1.8.2'; Scope = @(); Status = 'missing' }
    }
    if ($Tool.Name -eq 'dotnet') {
        return @{ Package = 'Microsoft.DotNet.SDK.10'; Installed = $null; Rows = @('8.0.1', '10.0.1'); Available = '10.0.1'; Scope = @('machine'); Status = 'multiple' }
    }
    return @{ Package = 'astral-sh.uv'; Installed = '0.12.4'; Rows = @('0.12.4'); Available = '0.12.4'; Scope = @('user', 'machine'); Status = 'current' }
}
function Add-ToolNote { param([hashtable]$Tool, [hashtable]$State) if ($Tool.Name -eq 'jq') { note 'jq' $Awkward } }
$NOTES = @()
$NOTE_TEXTS = @()
$JSON_OUTPUT = $true
$ELEVATED = $true
$SELECTED = @('jq', 'uv', 'dotnet')
if ($Mode) {
    $ACTIONS = [ordered]@{ report = $false; install = $false; list = $false }
    $ACTIONS[$Mode] = $true
    Resolve-Mode
} else {
    Show-Report
}
"""


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows installer's own functions"
)
class TestWindowsJsonReport(unittest.TestCase):
    """`install-tools.ps1 -Json`, driven through the script's own functions with winget stubbed."""

    def run_harness(self, mode: str = "") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory, "harness.ps1")
            harness.write_text(WINDOWS_HARNESS, encoding="utf-8")
            return subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(harness),
                    "-Installer",
                    str(WINDOWS_INSTALLER),
                    "-Awkward",
                    AWKWARD,
                    "-Mode",
                    mode,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )

    def test_report_writes_each_row_and_files_each_note(self) -> None:
        result = self.run_harness()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.isascii(), result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["platform"], "windows")
        self.assertEqual(
            report["tools"],
            [
                {
                    "tool": "jq",
                    "installed": None,
                    "available": "1.8.2",
                    "source": "jqlang.jq",
                    "mechanism": "winget",
                    "status": "missing",
                    "scope": [],
                    "notes": [AWKWARD],
                },
                {
                    "tool": "uv",
                    "installed": "0.12.4",
                    "available": "0.12.4",
                    "source": "astral-sh.uv",
                    "mechanism": "winget",
                    "status": "current",
                    "scope": ["user", "machine"],
                    "notes": [],
                },
                {
                    "tool": "dotnet",
                    "installed": None,
                    "available": "10.0.1",
                    "source": "Microsoft.DotNet.SDK.10",
                    "mechanism": "winget",
                    "status": "multiple",
                    "scope": ["machine"],
                    "notes": [],
                },
            ],
        )
        self.assertEqual(len(report["notes"]), 1)
        self.assertIn("elevated", report["notes"][0])

    def test_json_is_refused_beside_another_action(self) -> None:
        for mode in ("install", "list"):
            with self.subTest(mode=mode):
                result = self.run_harness(mode)
                self.assertEqual(result.returncode, 1)
                self.assertIn("-Json", result.stderr)


WINGET_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('Read-WingetTable', 'Test-WingetVersion', 'Resolve-InstalledVersion', 'Get-VersionKey', 'Compare-HostVersion', 'Get-ToolStatus')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$script:EXPLICIT = @()
function Get-ExplicitUpgrade { return $script:EXPLICIT }
$results = [ordered]@{}
foreach ($case in (Get-Content -Raw -LiteralPath $Cases | ConvertFrom-Json).PSObject.Properties) {
    $script:EXPLICIT = @(if ($case.Value.PSObject.Properties['explicit']) { $case.Value.explicit })
    $rows = Read-WingetTable -Text $case.Value.table -Id 'jqlang.jq'
    $installed = Resolve-InstalledVersion -Version $rows
    $state = @{ Readable = $true; Rows = $rows; Installed = $installed; Available = '1.8.2'; Package = 'jqlang.jq' }
    $status = Get-ToolStatus -Tool @{ Name = 'jq'; Probe = 'jq' } -State $state
    $results[$case.Name] = [ordered]@{ rows = @($rows); installed = $installed; status = $status }
}
$results | ConvertTo-Json -Depth 4
"""

UNREAD_APPLY_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('Show-Report', 'Invoke-ToolApply', 'Get-NoteText', 'note', 'Add-PathLengthNote', 'Get-PathLengthWarning')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
function log { param([string]$Message = '') Write-Host "LOG $Message" }
function info { param([string]$Message) Write-Host "INFO $Message" }
function warn { param([string]$Message) Write-Host "WARN $Message" }
function die { param([string]$Message) throw $Message }
function Get-HostPathLength { 0 }
function Get-Tool { param([string]$Name) @{ Name = $Name; Package = 'jqlang.jq'; Probe = 'jq'; Optional = @() } }
function Get-ToolState {
    param([hashtable]$Tool)
    return @{ Package = 'jqlang.jq'; Installed = $null; Rows = @('Unknown'); Readable = $true; Available = '1.8.2'; Scope = @('user'); Status = 'unknown' }
}
function Add-ToolNote { param([hashtable]$Tool, [hashtable]$State) }
function Invoke-WingetInstall { param([string]$Id) Write-Host "INSTALL $Id"; return 0 }
function Invoke-WingetUpgrade { param([string]$Id) Write-Host "UPGRADE $Id"; return 0 }
$NOTES = @()
$NOTE_TEXTS = @()
$JSON_OUTPUT = $false
$ELEVATED = $true
$SELECTED = @('jq')
$FAILED = @()
$WANT_SCOPE = $null
$WITH_OPTIONAL = $false
$MODE = 'install'
Show-Report
Invoke-ToolApply -ToolName 'jq'
"""


def winget_list(*versions: str) -> str:
    """A `winget list` table carrying one row per version for the id the harness asks about."""
    lines = [
        "Name   Id         Version  Available Source",
        "-------------------------------------------",
        *(f"jq     jqlang.jq  {version:<8} 1.8.2     winget" for version in versions),
    ]
    return "\n".join(lines)


def run_pwsh_harness(harness_text: str, cases: object) -> subprocess.CompletedProcess[str]:
    """Run a harness against the Windows installer, handing it `cases` as a JSON file."""
    with tempfile.TemporaryDirectory() as directory:
        harness = Path(directory, "harness.ps1")
        harness.write_text(harness_text, encoding="utf-8")
        data = Path(directory, "cases.json")
        data.write_text(json.dumps(cases), encoding="utf-8")
        return subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(harness),
                "-Installer",
                str(WINDOWS_INSTALLER),
                "-Cases",
                str(data),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=120,
        )


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows installer's own functions"
)
class TestWindowsInstalledVersion(unittest.TestCase):
    """A Version column token that is not a version leaves the installed version unread."""

    def test_a_non_version_token_is_unread_rather_than_installed(self) -> None:
        cases = {
            "unknown": {"table": winget_list("Unknown")},
            "below": {"table": winget_list("<")},
            "above": {"table": winget_list(">")},
            "unknown beside a version": {"table": winget_list("Unknown", "1.8.1")},
            "unknown and self-updating": {
                "table": winget_list("Unknown"),
                "explicit": ["jqlang.jq"],
            },
            "one version": {"table": winget_list("1.8.1")},
            "majors differ": {"table": winget_list("1.8.1", "2.0.0")},
        }
        result = run_pwsh_harness(WINGET_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "unknown": {"rows": ["Unknown"], "installed": None, "status": "unknown"},
                "below": {"rows": ["<"], "installed": None, "status": "unknown"},
                "above": {"rows": [">"], "installed": None, "status": "unknown"},
                "unknown beside a version": {
                    "rows": ["Unknown", "1.8.1"],
                    "installed": None,
                    "status": "unknown",
                },
                "unknown and self-updating": {
                    "rows": ["Unknown"],
                    "installed": None,
                    "status": "self-updating",
                },
                "one version": {"rows": ["1.8.1"], "installed": "1.8.1", "status": "outdated"},
                "majors differ": {
                    "rows": ["1.8.1", "2.0.0"],
                    "installed": None,
                    "status": "multiple",
                },
            },
        )

    def test_the_report_prints_the_row_and_install_leaves_the_tool_alone(self) -> None:
        result = run_pwsh_harness(UNREAD_APPLY_HARNESS, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        reported = [
            line.split() for line in result.stdout.splitlines() if line.startswith("LOG jq ")
        ]
        self.assertEqual([row[:3] for row in reported], [["LOG", "jq", "Unknown"]], result.stdout)
        self.assertIn("no version comparison is possible", result.stdout)
        self.assertNotIn("INSTALL ", result.stdout)
        self.assertNotIn("UPGRADE ", result.stdout)


# The registry and the optional list are the script's own assignments, read out of its syntax tree rather than restated.
SELECTION_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('Resolve-Selection', 'Show-List', 'Get-Tool')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$assigned = @('DOTNET_MAJOR', 'TOOLS', 'OPTIONAL_TOOLS')
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.AssignmentStatementAst] -and $assigned -contains $statement.Left.VariablePath.UserPath) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
function die { param([string]$Message) throw $Message }
$script:LINES = @()
function log { param([string]$Message = '') $script:LINES += $Message }
$results = [ordered]@{}
foreach ($case in (Get-Content -Raw -LiteralPath $Cases | ConvertFrom-Json).PSObject.Properties) {
    $script:WANT_TOOLS = @($case.Value.want)
    $script:WITH_OPTIONAL = [bool]$case.Value.optional
    $script:MODE = $case.Value.mode
    $script:SELECTED = Resolve-Selection
    $script:LINES = @()
    if ($script:MODE -eq 'list') { Show-List }
    $results[$case.Name] = [ordered]@{ selected = @($script:SELECTED); lines = @($script:LINES) }
}
$results | ConvertTo-Json -Depth 4
"""


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows installer's own functions"
)
class TestWindowsOptionalTools(unittest.TestCase):
    """An optional tool joins the default selection only under -Optional, as on Linux, and -List shows it."""

    def setUp(self) -> None:
        cases = {
            "default": {"want": [], "optional": False, "mode": "report"},
            "optional": {"want": [], "optional": True, "mode": "install"},
            "named": {"want": ["op"], "optional": False, "mode": "install"},
            "list": {"want": [], "optional": False, "mode": "list"},
        }
        result = run_pwsh_harness(SELECTION_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.results = json.loads(result.stdout)

    def test_the_default_selection_leaves_the_optional_tool_out(self) -> None:
        selected = self.results["default"]["selected"]
        self.assertNotIn("op", selected)
        self.assertIn("gh", selected)

    def test_optional_brings_it_in_and_naming_it_selects_it(self) -> None:
        self.assertIn("op", self.results["optional"]["selected"])
        self.assertEqual(self.results["named"]["selected"], ["op"])

    def test_the_list_shows_it_and_marks_it_as_the_optional_tool(self) -> None:
        self.assertIn("op", self.results["list"]["selected"])
        rows = {line.split()[0]: line for line in self.results["list"]["lines"][1:]}
        self.assertIn("AgileBits.1Password.CLI", rows["op"])
        self.assertIn("(optional tool)", rows["op"])
        self.assertNotIn("(optional tool)", rows["gh"])


DOTNET_SDK_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('Read-DotnetSdkList', 'Get-UntrackedDotnetSdk', 'Get-VersionKey', 'Compare-HostVersion')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$results = [ordered]@{}
foreach ($case in (Get-Content -Raw -LiteralPath $Cases | ConvertFrom-Json).PSObject.Properties) {
    $sdk = Read-DotnetSdkList -Line @($case.Value.listing)
    $results[$case.Name] = [ordered]@{ sdk = $sdk; untracked = (Get-UntrackedDotnetSdk -Installed $case.Value.installed -Sdk $sdk) }
}
$results | ConvertTo-Json -Depth 4
"""


def dotnet_sdks(*versions: str) -> list[str]:
    """The lines `dotnet --list-sdks` prints, one per version, each beside a made-up sdk directory."""
    return [f"{version} [X:\\dotnet\\sdk]" for version in versions]


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows installer's own functions"
)
class TestWindowsUntrackedDotnetSdk(unittest.TestCase):
    """An SDK newer than the one winget installed is named, whoever installed it."""

    def test_only_an_sdk_newer_than_winget_s_copy_is_named(self) -> None:
        cases = {
            "preview ahead of winget": {
                "installed": "10.0.303",
                "listing": dotnet_sdks("9.0.205", "10.0.303", "10.0.400-preview.0.1", "8.0.319"),
            },
            "newest of several ahead": {
                "installed": "10.0.111",
                "listing": dotnet_sdks("10.0.400-preview.0.1", "10.0.204", "10.0.303"),
            },
            "a release beside its own preview": {
                "installed": "10.0.303",
                "listing": dotnet_sdks("10.0.400-preview.0.1", "10.0.400"),
            },
            "a release listed ahead of its own preview": {
                "installed": "10.0.303",
                "listing": dotnet_sdks("10.0.400", "10.0.400-preview.0.1"),
            },
            "the later of two prereleases": {
                "installed": "10.0.303",
                "listing": dotnet_sdks("10.0.400-preview.0.1", "10.0.400-rc.2.1"),
            },
            "winget holds the newest": {
                "installed": "10.0.303",
                "listing": dotnet_sdks("8.0.319", "10.0.204", "10.0.303"),
            },
            "a preview of winget's own release": {
                "installed": "10.0.400",
                "listing": dotnet_sdks("10.0.400-rc.1.2", "10.0.400"),
            },
            "winget version unread": {
                "installed": "",
                "listing": dotnet_sdks("10.0.400"),
            },
            "listing carries no sdk": {
                "installed": "10.0.303",
                "listing": ["", "No .NET SDKs were found."],
            },
        }
        result = run_pwsh_harness(DOTNET_SDK_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            {name: value["untracked"] for name, value in json.loads(result.stdout).items()},
            {
                "preview ahead of winget": "10.0.400-preview.0.1",
                "newest of several ahead": "10.0.400-preview.0.1",
                "a release beside its own preview": "10.0.400",
                "a release listed ahead of its own preview": "10.0.400",
                "the later of two prereleases": "10.0.400-rc.2.1",
                "winget holds the newest": None,
                "a preview of winget's own release": None,
                "winget version unread": None,
                "listing carries no sdk": None,
            },
        )

    def test_the_listing_is_read_down_to_its_versions(self) -> None:
        cases = {"listing": {"installed": "", "listing": dotnet_sdks("8.0.319", "10.0.100-rc.1")}}
        result = run_pwsh_harness(DOTNET_SDK_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["listing"]["sdk"], ["8.0.319", "10.0.100-rc.1"])


DOTNET_NOTE_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$PSNativeCommandUseErrorActionPreference = $false
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('Add-ToolNote', 'note', 'Get-DotnetSdk', 'Read-DotnetSdkList', 'Get-UntrackedDotnetSdk', 'Get-VersionKey', 'Compare-HostVersion')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$WITH_OPTIONAL = $true
$tool = @{ Name = 'dotnet'; Package = 'Microsoft.DotNet.SDK.10'; Probe = 'dotnet'; Optional = @() }
$results = [ordered]@{}
foreach ($case in (Get-Content -Raw -LiteralPath $Cases | ConvertFrom-Json).PSObject.Properties) {
    $env:PATH = $case.Value.path
    $NOTES = @()
    $NOTE_TEXTS = @()
    $state = @{ Package = 'Microsoft.DotNet.SDK.10'; Installed = $case.Value.installed; Rows = @(); Scope = @(); Status = 'current' }
    Add-ToolNote -Tool $tool -State $state
    $results[$case.Name] = @($NOTES)
}
$results | ConvertTo-Json -Depth 4
"""


def write_dotnet_stub(directory: Path, exit_code: int, *versions: str) -> None:
    """A `dotnet` on `directory` that prints `versions` as `--list-sdks` does, then exits `exit_code`.

    Any other argument exits 9 printing nothing, so a caller asking for the wrong listing reads none.
    """
    if sys.platform == "win32":
        lines = [
            "@echo off",
            'if not "%~1"=="--list-sdks" exit /b 9',
            *(f"echo {version} [X:\\dotnet\\sdk]" for version in versions),
        ]
        Path(directory, "dotnet.cmd").write_text(
            "\r\n".join([*lines, f"exit /b {exit_code}", ""]), encoding="ascii"
        )
    else:
        lines = [
            "#!/bin/sh",
            '[ "$1" = --list-sdks ] || exit 9',
            *(f"echo '{version} [/dotnet/sdk]'" for version in versions),
        ]
        stub = Path(directory, "dotnet")
        stub.write_text("\n".join([*lines, f"exit {exit_code}", ""]), encoding="ascii")
        stub.chmod(0o755)


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows installer's own functions"
)
class TestWindowsUntrackedDotnetNote(unittest.TestCase):
    """The dotnet row's note, driven through `Add-ToolNote` with a stub `dotnet` alone on PATH."""

    def test_the_note_follows_what_dotnet_lists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = {name: Path(directory, name) for name in ("newer", "failing", "absent")}
            for path in paths.values():
                path.mkdir()
            write_dotnet_stub(paths["newer"], 0, "10.0.303", "10.0.400-preview.0.1", "10.0.400")
            write_dotnet_stub(paths["failing"], 3, "10.0.400")
            cases = {
                "newer": {"path": str(paths["newer"]), "installed": "10.0.303"},
                "failing": {"path": str(paths["failing"]), "installed": "10.0.303"},
                "absent": {"path": str(paths["absent"]), "installed": "10.0.303"},
                "unread": {"path": str(paths["newer"]), "installed": ""},
            }
            result = run_pwsh_harness(DOTNET_NOTE_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        notes = json.loads(result.stdout)
        self.assertEqual(len(notes["newer"]), 1, notes)
        self.assertTrue(notes["newer"][0].startswith("dotnet: dotnet --list-sdks holds 10.0.400,"))
        self.assertIn("newer than the 10.0.303 winget installed", notes["newer"][0])
        self.assertIn("other than Microsoft.DotNet.SDK.10", notes["newer"][0])
        self.assertEqual(
            {name: notes[name] for name in ("failing", "absent", "unread")},
            {"failing": [], "absent": [], "unread": []},
        )


FAILURE_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('Get-InstallerFailureReason', 'Select-FailureEntry', 'Get-InstallRoot', 'Invoke-Utf8Run', 'Get-ToolHolder')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$case = Get-Content -Raw -LiteralPath $Cases | ConvertFrom-Json
$results = [ordered]@{}
foreach ($probe in $case.reasons) {
    $reason = Get-InstallerFailureReason -Id $probe.id -Since ([datetime]$probe.since) -Directory $case.directory
    $results["reason $($probe.id) $($probe.since)"] = [ordered]@{
        log = [bool]$reason.Log
        entries = @($reason.Entries)
        winget_log = [bool]$reason.WingetLog
        winget_entries = @($reason.WingetEntries)
        in_use = $reason.InUse
    }
}
$held = [System.IO.File]::Open((Join-Path $case.directory $case.locked), 'Open', 'Read', 'None')
try {
    $reason = Get-InstallerFailureReason -Id 'Locked.Tool' -Since ([datetime]'2000-01-01') -Directory $case.directory
    $results['locked'] = [ordered]@{ winget_log = [bool]$reason.WingetLog; winget_entries = @($reason.WingetEntries) }
} finally {
    $held.Dispose()
}
$env:ProgramFiles = 'C:\PF'
${env:ProgramFiles(x86)} = 'C:\PF86'
$env:LOCALAPPDATA = 'C:\LAD'
$results['roots'] = @($case.roots | ForEach-Object { [string](Get-InstallRoot -Path $_) })
$results['absent holders'] = @(
    (Get-ToolHolder -Probe 'no-such-tool-on-path').Count
    (Get-ToolHolder -Probe 'no-such-tool-on-path' -Executable '').Count
)
if ($IsWindows) {
    # This harness runs as pwsh from a directory none of the containers above name, so the name fallback finds it, and a tree read from a file elsewhere does not.
    # Read back through Get-Item, which expands an 8.3 short path such as a hosted runner's temp directory, so the container and the file it holds are compared in the same form.
    $env:LOCALAPPDATA = Join-Path (Get-Item -LiteralPath $case.directory).FullName 'LAD'
    $elsewhere = Join-Path $env:LOCALAPPDATA 'Programs\Tool\tool.exe'
    $byName = Get-ToolHolder -Probe 'pwsh'
    $byTree = Get-ToolHolder -Probe 'pwsh' -Executable $elsewhere
    $results['scoped holders'] = @(
        [bool]($byName | Where-Object { $_.Id -eq $PID })
        [bool]($byTree | Where-Object { $_.Id -eq $PID })
        $byTree.Count
    )
}
$before = [Console]::OutputEncoding
$switchable = $true
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); [Console]::OutputEncoding = $before } catch { $switchable = $false }
function run { param([string]$Command, [string[]]$Arguments) $script:INSIDE = [Console]::OutputEncoding.WebName; return 5 }
foreach ($console in @($true, $false)) {
    $script:CONSOLE = $console
    function Test-OutputToConsole { return $script:CONSOLE }
    $script:INSIDE = $null
    $results["utf8 console=$console"] = [ordered]@{
        code = (Invoke-Utf8Run 'docker' @('desktop', 'stop'))
        switchable = $switchable
        before = $before.WebName
        inside = $script:INSIDE
        restored = ([Console]::OutputEncoding.WebName -eq $before.WebName)
    }
}
$results | ConvertTo-Json -Depth 4
"""

WRITE_FAILURE_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('Write-InstallFailure')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$script:LINES = @()
function warn { param([string]$Message) $script:LINES += "WARN $Message" }
function info { param([string]$Message) $script:LINES += "INFO $Message" }
function Hide-Home { param([string]$Path) return 'LOG' }
function Get-ToolHolder {
    param([string]$Probe, [string]$Executable)
    $script:LINES += "HOLDERS $Probe $Executable".TrimEnd()
    return , @([pscustomobject]@{ ProcessName = 'helper'; Id = 1234 }, [pscustomobject]@{ ProcessName = 'bash'; Id = 99 })
}
$results = [ordered]@{}
foreach ($case in (Get-Content -Raw -LiteralPath $Cases | ConvertFrom-Json).PSObject.Properties) {
    $script:REASON = @{
        Log = 'C:\log.log'
        Entries = @($case.Value.entries | ForEach-Object { $_ -replace '<HOME>', $HOME.Replace('$', '$$') })
        WingetLog = 'C:\winget.log'
        WingetEntries = @(if ($case.Value.PSObject.Properties['winget_entries']) { $case.Value.winget_entries | ForEach-Object { $_ -replace '<HOME>', $HOME.Replace('$', '$$') } })
        InUse = [bool]($case.Value.PSObject.Properties['in_use'] -and $case.Value.in_use)
    }
    if ($script:REASON.Entries.Count -eq 0) { $script:REASON.Log = $null }
    if ($script:REASON.WingetEntries.Count -eq 0) { $script:REASON.WingetLog = $null }
    function Get-InstallerFailureReason { param([string]$Id, [datetime]$Since) return $script:REASON }
    $script:LINES = @()
    Write-InstallFailure -ToolName 'tool' -Package 'Example.Tool' -Code $case.Value.code -Since (Get-Date) -Probe 'tool' -Executable $case.Value.executable
    $results[$case.Name] = @($script:LINES | ForEach-Object { $_.Replace($HOME, '<HOME>') })
}
$results | ConvertTo-Json -Depth 4
"""

INNO_LOG = [
    "2026-01-02 03:04:05.100   Log opened.",
    "2026-01-02 03:04:05.300   Successfully imported the DLL function. Delay loaded? No",
    "2026-01-02 03:04:06.000   Defaulting to Cancel for suppressed message box (Retry/Cancel):",
    "                          The following process(es) use Example Tool:",
    "                          ",
    "                          helper.exe (PID 1234)",
    "                          Please terminate those processes and retry.",
    "2026-01-02 03:04:06.001   Failed to proceed to next wizard page; aborting.",
    "2026-01-02 03:04:06.002   Deinitializing Setup.",
]

PORTABLE_WINGET_LOG = [
    "2026-01-02 03:04:05.100 <I> [CLI ] WinGet invoked with arguments: 'upgrade' '--id' 'Portable.Tool'",
    "2026-01-02 03:04:05.200 <I> [CLI ] Deleting portable exe at: C:\\LAD\\Portable.Tool_x\\tool.exe",
    (
        "2026-01-02 03:04:05.300 <E> [CLI ] Caught std::exception: remove: Access is denied.:"
        ' "C:\\LAD\\Portable.Tool_x\\tool.exe"'
    ),
    "2026-01-02 03:04:05.301 <E> [CLI ] Portable installer failed: 2316632067",
    "2026-01-02 03:04:05.302 <E> [CLI ] Terminating context: 0x8a150052 at C:\\src\\InstallFlow.cpp:239",
]

OTHER_WINGET_LOG = [
    "2026-01-02 03:04:05.100 <I> [CLI ] WinGet invoked with arguments: 'upgrade' '--id' 'Portable.Tool.Extra'",
    "2026-01-02 03:04:05.300 <E> [CLI ] Caught std::exception: remove: Access is denied.",
]

HYPHEN_NEIGHBOR_WINGET_LOG = [
    "2026-01-02 03:04:05.100 <I> [CLI ] WinGet invoked with arguments: 'upgrade' '--id' 'Portable.Tool-Extra'",
    "2026-01-02 03:04:05.200 <I> [CLI ] WinGet invoked with arguments: 'upgrade' '--id' 'Vendor-Portable.Tool'",
    "2026-01-02 03:04:05.300 <E> [CLI ] A neighboring package's own failure.",
]

READABLE_WINGET_LOG = [
    "2026-01-02 03:04:05.100 <I> [CLI ] WinGet invoked with arguments: 'upgrade' '--id' 'Locked.Tool'",
    "2026-01-02 03:04:05.300 <E> [CLI ] The readable log's failure.",
]

LOCKED_WINGET_LOG = [
    "2026-01-02 03:04:05.100 <I> [CLI ] WinGet invoked with arguments: 'upgrade' '--id' 'Locked.Tool'",
    "2026-01-02 03:04:05.300 <E> [CLI ] The locked log's failure.",
]

UNRELATED_DENIAL_WINGET_LOG = [
    "2026-01-02 03:04:05.100 <I> [CLI ] WinGet invoked with arguments: 'install' '--id' 'Settings.Tool'",
    "2026-01-02 03:04:05.300 <E> [CORE] Failed to open the settings store: Access is denied.",
]

DENIED_LOG = [
    "2026-01-02 03:04:05.100   Error 0x80070005: Access is denied.",
]

BUSY_LOG = [
    "2026-01-02 03:04:05.100   The file is being used by another process.",
    *(
        f"2026-01-02 03:04:06.00{index}   Error {index}: rollback step failed."
        for index in range(5)
    ),
]


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows installer's own functions"
)
class TestWindowsInstallFailureReason(unittest.TestCase):
    """The installer log's reason, a tool's install root, and the UTF-8 run's console handling."""

    def run_cases(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory, "Example.Tool.1.2.3-26-01-02-03-04-05.log")
            log.write_text("\r\n".join([*INNO_LOG, ""]), encoding="utf-8", newline="")
            busy = Path(directory, "Busy.Tool.1.0-26-01-02-03-04-05.log")
            busy.write_text("\r\n".join([*BUSY_LOG, ""]), encoding="utf-8", newline="")
            denied = Path(directory, "Denied.Tool.1.0-26-01-02-03-04-05.log")
            denied.write_text("\r\n".join([*DENIED_LOG, ""]), encoding="utf-8", newline="")
            for name, lines in (
                ("WinGet-2026-01-02-03-04-05.000.log", PORTABLE_WINGET_LOG),
                ("WinGet-2026-01-02-03-04-06.000.log", OTHER_WINGET_LOG),
                ("WinGet-2026-01-02-03-04-07.000.log", UNRELATED_DENIAL_WINGET_LOG),
                ("WinGet-2026-01-02-03-04-08.000.log", HYPHEN_NEIGHBOR_WINGET_LOG),
                ("WinGet-2026-01-02-03-04-09.000.log", READABLE_WINGET_LOG),
                ("WinGet-2026-01-02-03-04-10.000.log", LOCKED_WINGET_LOG),
            ):
                Path(directory, name).write_text(
                    "\r\n".join([*lines, ""]), encoding="utf-8", newline=""
                )
            elsewhere = Path(directory, "LAD", "Programs", "Tool", "tool.exe")
            elsewhere.parent.mkdir(parents=True)
            elsewhere.write_bytes(b"")
            cases = {
                "directory": directory,
                "locked": "WinGet-2026-01-02-03-04-10.000.log",
                "reasons": [
                    {"id": "Example.Tool", "since": "2000-01-01"},
                    {"id": "Example.Tool", "since": "2100-01-01"},
                    {"id": "Other.Tool", "since": "2000-01-01"},
                    {"id": "Busy.Tool", "since": "2000-01-01"},
                    {"id": "Denied.Tool", "since": "2000-01-01"},
                    {"id": "Settings.Tool", "since": "2000-01-01"},
                    {"id": "Portable.Tool", "since": "2000-01-01"},
                    {"id": "Portable.Tool", "since": "2100-01-01"},
                ],
                "roots": [
                    "C:\\PF\\Git\\cmd\\git.exe",
                    "C:\\PF\\Git\\mingw64\\bin\\git.exe",
                    "C:\\PF86\\Tool\\tool.exe",
                    "C:\\LAD\\Microsoft\\WinGet\\Packages\\Example.Tool_x\\tool.exe",
                    "C:\\PF\\WinGet\\Packages\\Example.Tool_x\\tool.exe",
                    "C:\\LAD\\Programs\\Python\\Python313\\python.exe",
                    "C:\\Windows\\py.exe",
                    "C:\\PF\\loose.exe",
                ],
            }
            result = run_pwsh_harness(FAILURE_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_the_log_entry_keeps_its_continuation_lines(self) -> None:
        results = self.run_cases()
        self.assertEqual(
            results["reason Example.Tool 2000-01-01"],
            {
                "log": True,
                "entries": [
                    (
                        "Defaulting to Cancel for suppressed message box (Retry/Cancel): The"
                        " following process(es) use Example Tool: helper.exe (PID 1234) Please"
                        " terminate those processes and retry."
                    ),
                    "Failed to proceed to next wizard page; aborting.",
                ],
                "winget_log": False,
                "winget_entries": [],
                "in_use": True,
            },
        )

    def test_an_in_use_entry_survives_the_cap_on_later_errors(self) -> None:
        self.assertEqual(
            self.run_cases()["reason Busy.Tool 2000-01-01"],
            {
                "log": True,
                "entries": [
                    "The file is being used by another process.",
                    "Error 2: rollback step failed.",
                    "Error 3: rollback step failed.",
                    "Error 4: rollback step failed.",
                ],
                "winget_log": False,
                "winget_entries": [],
                "in_use": True,
            },
        )

    def test_an_installer_logs_access_denial_alone_is_not_files_in_use(self) -> None:
        """An installer's denial can be a permission failure, which no running process causes."""
        self.assertEqual(
            self.run_cases()["reason Denied.Tool 2000-01-01"],
            {
                "log": True,
                "entries": ["Error 0x80070005: Access is denied."],
                "winget_log": False,
                "winget_entries": [],
                "in_use": False,
            },
        )

    def test_a_winget_log_nothing_can_read_leaves_the_readable_one_to_report(self) -> None:
        """A locked log is skipped like an absent one, so the failure report still runs."""
        self.assertEqual(
            self.run_cases()["locked"],
            {"winget_log": True, "winget_entries": ["The readable log's failure."]},
        )

    def test_a_winget_denial_naming_no_file_is_not_files_in_use(self) -> None:
        """Only a denial naming a file is winget failing to replace one."""
        self.assertEqual(
            self.run_cases()["reason Settings.Tool 2000-01-01"],
            {
                "log": False,
                "entries": [],
                "winget_log": True,
                "winget_entries": ["Failed to open the settings store: Access is denied."],
                "in_use": False,
            },
        )

    def test_a_log_older_than_the_attempt_or_for_another_id_is_not_read(self) -> None:
        results = self.run_cases()
        unread = {
            "log": False,
            "entries": [],
            "winget_log": False,
            "winget_entries": [],
            "in_use": False,
        }
        self.assertEqual(results["reason Example.Tool 2100-01-01"], unread)
        self.assertEqual(results["reason Other.Tool 2000-01-01"], unread)
        self.assertEqual(results["reason Portable.Tool 2100-01-01"], unread)

    def test_wingets_own_log_gives_a_portable_packages_reason(self) -> None:
        """A package with no installer log has its reason read from winget's own session log.

        A session log naming a longer id that only starts with this one is not read as this one's.
        """
        self.assertEqual(
            self.run_cases()["reason Portable.Tool 2000-01-01"],
            {
                "log": False,
                "entries": [],
                "winget_log": True,
                "winget_entries": [
                    (
                        "Caught std::exception: remove: Access is denied.:"
                        ' "C:\\LAD\\Portable.Tool_x\\tool.exe"'
                    ),
                    "Portable installer failed: 2316632067",
                    "Terminating context: 0x8a150052 at C:\\src\\InstallFlow.cpp:239",
                ],
                "in_use": True,
            },
        )

    @unittest.skipUnless(sys.platform == "win32", "reads this host's own process list")
    def test_a_same_named_process_outside_the_tree_is_not_a_holder(self) -> None:
        self.assertEqual(self.run_cases()["scoped holders"], [True, False, 0])

    def test_the_install_root_is_the_containers_child_whichever_copy_resolved(self) -> None:
        self.assertEqual(
            self.run_cases()["roots"],
            [
                "C:\\PF\\Git",
                "C:\\PF\\Git",
                "C:\\PF86\\Tool",
                "C:\\LAD\\Microsoft\\WinGet\\Packages\\Example.Tool_x",
                "C:\\PF\\WinGet\\Packages\\Example.Tool_x",
                "C:\\LAD\\Programs\\Python\\Python313",
                "",
                "",
            ],
        )

    def test_a_tool_off_path_has_no_holders_rather_than_failing(self) -> None:
        self.assertEqual(self.run_cases()["absent holders"], [0, 0])

    def test_the_utf8_run_switches_only_for_console_output_and_restores_it(self) -> None:
        results = self.run_cases()
        console = results["utf8 console=True"]
        redirected = results["utf8 console=False"]
        for utf8 in (console, redirected):
            self.assertEqual(utf8["code"], 5)
            self.assertTrue(utf8["restored"])
        self.assertEqual(redirected["inside"], redirected["before"])
        if console["switchable"]:
            self.assertEqual(console["inside"], "utf-8")


INSTALLER_FAILED = -1978335226
SHARING_VIOLATION = -2147024864
WINGET_FILE_IN_USE = -1978334973
WINGET_INSTALL_FAILED = -1978335150


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows installer's own functions"
)
class TestWindowsInstallFailureReport(unittest.TestCase):
    """`Write-InstallFailure` blames running processes only where something says files were in use."""

    def run_cases(self) -> dict[str, list[str]]:
        cases = {
            "in use": {
                "entries": ["The following process(es) use Example Tool: helper.exe (PID 1234)"],
                "in_use": True,
                "code": INSTALLER_FAILED,
                "executable": "",
            },
            "unrelated": {
                "entries": [
                    "Error 0x80070666: Process returned error at <HOME>\\AppData\\Local\\x.log"
                ],
                "code": INSTALLER_FAILED,
                "executable": "",
            },
            "no reason": {"entries": [], "code": INSTALLER_FAILED, "executable": ""},
            "winget file in use": {"entries": [], "code": WINGET_FILE_IN_USE, "executable": ""},
            "portable in use": {
                "entries": [],
                "winget_entries": [
                    'Caught std::exception: remove: Access is denied.: "<HOME>\\Tool_x\\tool.exe"'
                ],
                "in_use": True,
                "code": WINGET_INSTALL_FAILED,
                "executable": "",
            },
            "sharing violation": {
                "entries": [],
                "code": SHARING_VIOLATION,
                "executable": "C:\\Tool\\tool.exe",
            },
        }
        result = run_pwsh_harness(WRITE_FAILURE_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded: dict[str, list[str]] = json.loads(result.stdout)
        return loaded

    def test_files_in_use_names_the_holders_as_the_cause(self) -> None:
        self.assertEqual(
            self.run_cases()["in use"],
            [
                "WARN tool failed on Example.Tool, winget exited -1978335226 (0x8A150006)",
                "INFO The installer log says:",
                "INFO   The following process(es) use Example Tool: helper.exe (PID 1234)",
                "INFO Full installer log: LOG",
                "HOLDERS tool",
                (
                    "INFO Running from tool's install, and Windows cannot replace a file a"
                    " running process holds:"
                ),
                "INFO   helper (process 1234)",
                "INFO   bash (process 99)",
                "INFO Close them, or stop them with the command below, then run this again:",
                "INFO   Stop-Process -Id 1234, 99",
            ],
        )

    def test_an_unrelated_reason_blames_no_process_and_masks_the_home_folder(self) -> None:
        self.assertEqual(
            self.run_cases()["unrelated"],
            [
                "WARN tool failed on Example.Tool, winget exited -1978335226 (0x8A150006)",
                "INFO The installer log says:",
                "INFO   Error 0x80070666: Process returned error at %USERPROFILE%\\AppData\\Local\\x.log",
                "INFO Full installer log: LOG",
            ],
        )

    def test_no_reason_blames_no_process(self) -> None:
        self.assertEqual(
            self.run_cases()["no reason"],
            ["WARN tool failed on Example.Tool, winget exited -1978335226 (0x8A150006)"],
        )

    def test_wingets_own_file_in_use_code_names_the_holders(self) -> None:
        lines = self.run_cases()["winget file in use"]
        self.assertEqual(
            lines[:2],
            [
                "WARN tool failed on Example.Tool, winget exited -1978334973 (0x8A150103)",
                "HOLDERS tool",
            ],
        )

    def test_wingets_own_log_saying_access_denied_names_the_holders(self) -> None:
        self.assertEqual(
            self.run_cases()["portable in use"],
            [
                "WARN tool failed on Example.Tool, winget exited -1978335150 (0x8A150052)",
                "INFO winget's own log says:",
                (
                    "INFO   Caught std::exception: remove: Access is denied.:"
                    ' "%USERPROFILE%\\Tool_x\\tool.exe"'
                ),
                "INFO Full winget log: LOG",
                "HOLDERS tool",
                (
                    "INFO Running from tool's install, and Windows cannot replace a file a"
                    " running process holds:"
                ),
                "INFO   helper (process 1234)",
                "INFO   bash (process 99)",
                "INFO Close them, or stop them with the command below, then run this again:",
                "INFO   Stop-Process -Id 1234, 99",
            ],
        )

    def test_a_sharing_violation_names_the_holders_from_the_executable_given(self) -> None:
        lines = self.run_cases()["sharing violation"]
        self.assertEqual(
            lines[:2],
            [
                "WARN tool failed on Example.Tool, winget exited -2147024864 (0x80070020)",
                "HOLDERS tool C:\\Tool\\tool.exe",
            ],
        )
        self.assertEqual(lines[-1], "INFO   Stop-Process -Id 1234, 99")


PATH_LENGTH_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$case = Get-Content -Raw -LiteralPath $Cases | ConvertFrom-Json
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($case.script, [ref]$tokens, [ref]$errors)
$definitions = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $case.wanted -contains $node.Name }, $true))
foreach ($definition in $definitions) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
function log { param([string]$Message = '') Write-Host "LOG $Message" }
function info { param([string]$Message) Write-Host "INFO $Message" }
function step { param([string]$Message) Write-Host "STEP $Message" }
function warn { param([string]$Message) Write-Host "WARN $Message" }
function confirm { param([string]$Message) return $true }
function Invoke-ToolApply { param([string]$ToolName) $script:GROWN = $true }
function Invoke-PackageUpgrade { $script:GROWN = $true }
function Get-HostDescription { 'a host' }
function Get-Upgradable { @{ Ready = @(); Explicit = @() } }
function Get-WslVersion { '2.0' }
function Get-DockerProcess { , @() }
function Read-PathScope { @{ Raw = ''; Kind = $null } }
function Get-PathCleanup { @{ Machine = @{ Removed = @(); Unjudged = @() }; User = @{ Removed = @(); Unjudged = @() } } }
$GROWN = $case.action -notlike 'Invoke-*'
if ($case.action -notin @('read', 'text')) {
    function Get-HostPathLength { if ($script:GROWN) { $case.length } else { 0 } }
}
$NOTES = @()
$NOTE_TEXTS = @()
$JSON_OUTPUT = $true
$ELEVATED = $false
$SELECTED = if ($case.action -eq 'Invoke-Apply') { @('jq') } else { @() }
$CHANGED = @()
$FAILED = @()
$MODE = 'packages'
$DRY_RUN = $false
switch ($case.action) {
    'warning' {
        $case.lengths | ForEach-Object { [ordered]@{ length = $_; warning = Get-PathLengthWarning -Length $_ } } | ConvertTo-Json -AsArray
    }
    'text' { $definitions | Sort-Object { $_.Name } | ForEach-Object { $_.Extent.Text } }
    'read' {
        $read = {
            param($hive, $subKey)
            $key = $hive.OpenSubKey($subKey)
            if ($key.GetValueNames() -notcontains 'Path') { return '' }
            $raw = $key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            if ($key.GetValueKind('Path') -eq 'ExpandString') { [Environment]::ExpandEnvironmentVariables($raw) } else { $raw }
        }
        $machine = & $read ([Microsoft.Win32.Registry]::LocalMachine) 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
        $user = & $read ([Microsoft.Win32.Registry]::CurrentUser) 'Environment'
        [ordered]@{ expected = "$machine;$user".Length; actual = Get-HostPathLength } | ConvertTo-Json
    }
    default { & $case.action }
}
"""


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows host scripts' own functions"
)
class TestWindowsPathLengthWarning(unittest.TestCase):
    """The warning above the margin, in both scripts, on every path that runs it.

    The stubbed installs are what grow the stubbed length.
    So an apply or an upgrade measuring before them reads 0 and prints no warning.
    """

    FUNCTIONS = ("Get-HostPathLength", "Get-PathLengthWarning")

    def drive(self, script: Path, action: str, length: int = 0, *wanted: str) -> str:
        cases = {
            "script": str(script),
            "action": action,
            "length": length,
            "wanted": [*self.FUNCTIONS, *wanted],
        }
        result = run_pwsh_harness(PATH_LENGTH_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_warning_starts_one_past_the_margin(self) -> None:
        cases = {
            "script": str(WINDOWS_INSTALLER),
            "action": "warning",
            "lengths": [0, 3700, 3701, 5000],
            "wanted": ["Get-PathLengthWarning"],
        }
        result = run_pwsh_harness(PATH_LENGTH_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {row["length"]: row["warning"] for row in json.loads(result.stdout)}
        self.assertIsNone(rows[0])
        self.assertIsNone(rows[3700])
        self.assertIn("expand to 3701 characters", rows[3701])
        self.assertIn("expand to 5000 characters", rows[5000])

    def test_both_scripts_carry_the_same_functions(self) -> None:
        installer = self.drive(WINDOWS_INSTALLER, "text")
        self.assertIn("function Get-HostPathLength", installer)
        self.assertIn("function Get-PathLengthWarning", installer)
        self.assertEqual(installer, self.drive(WINDOWS_UPGRADER, "text"))

    def test_the_report_files_it_as_a_note(self) -> None:
        wanted = ("Show-Report", "Add-PathLengthNote", "note", "Get-NoteText")
        over = json.loads(self.drive(WINDOWS_INSTALLER, "Show-Report", 3701, *wanted))
        self.assertEqual(len(over["notes"]), 1, over["notes"])
        self.assertIn("expand to 3701 characters", over["notes"][0])
        under = json.loads(self.drive(WINDOWS_INSTALLER, "Show-Report", 3700, *wanted))
        self.assertEqual(under["notes"], [])

    def test_an_apply_measures_after_its_installs_and_prints_it(self) -> None:
        wanted = ("Invoke-Apply", "Add-PathLengthNote", "note")
        over = self.drive(WINDOWS_INSTALLER, "Invoke-Apply", 3701, *wanted)
        self.assertIn("INFO path: the machine and user Path", over)
        under = self.drive(WINDOWS_INSTALLER, "Invoke-Apply", 3700, *wanted)
        self.assertNotIn("Path", under)

    def test_upgrade_host_warns_in_status_and_measures_after_an_upgrade(self) -> None:
        for action in ("Show-Status", "Invoke-Upgrade"):
            with self.subTest(action=action):
                wanted = (action, "Write-PathLengthWarning")
                over = self.drive(WINDOWS_UPGRADER, action, 3701, *wanted)
                self.assertIn("WARN the machine and user Path together expand to 3701", over)
                under = self.drive(WINDOWS_UPGRADER, action, 3700, *wanted)
                self.assertNotIn("WARN", under)

    @unittest.skipUnless(os.name == "nt", "reads the Windows registry")
    def test_the_length_is_the_expanded_registry_values_joined(self) -> None:
        for script in (WINDOWS_INSTALLER, WINDOWS_UPGRADER):
            with self.subTest(script=script.name):
                lengths = json.loads(self.drive(script, "read"))
                self.assertEqual(lengths["actual"], lengths["expected"])


if __name__ == "__main__":
    unittest.main()

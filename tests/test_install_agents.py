#!/usr/bin/env python3
"""Tests for `host-setup/linux/install-agents.sh`, the per-user agent tool updater.

The contract under test is the one a nightly run relies on:

- an upgrade drives only the tools already installed
- a tool a package manager owns is left to that manager
- nothing is installed unless it is named
- herdr's integrations and restart state reach the output

Each tool is a stub on a PATH of the test's own that records how it was called.
This is because the real tools update over the network and would change the host running the test.

Run as `python3 tests/test_install_agents.py`, or under `python3 -m unittest discover -s tests`.
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

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "host-setup" / "linux" / "install-agents.sh"

# The manager calls that only read, which the tests do not count as a change.
READS = {"npm root -g", "npm prefix -g", "mise ls --json"}

AWKWARD = 'a "quoted" back\\slash\ttab\nnewline\rreturn \x01\x1f é end'

# A stub answers --version with a banner shaped like its tool's own, records every call, and answers herdr's status reads from files the test writes.
STUB = """#!/bin/bash
printf '%s %s\\n' "$(basename "$0")" "$*" >>"$CALLS"
case "$*" in
--version) printf '%s\\n' "$BANNER" ;;
"integration status --outdated-only") cat "$STUB_DIR/outdated" 2>/dev/null || true ;;
"status --json") cat "$STUB_DIR/status.json" ;;
"root -g") cat "$STUB_DIR/npm_root" 2>/dev/null || true ;;
"prefix -g") cat "$STUB_DIR/npm_prefix" 2>/dev/null || true ;;
"ls --json") cat "$STUB_DIR/mise_ls.json" 2>/dev/null || true ;;
esac
exit 0
"""


# A stub whose updater installs a newer copy into a directory ahead of it on PATH, as an updater that relocates its binary does.
RELOCATING_STUB = """#!/bin/bash
printf '%s %s\\n' "$(basename "$0")" "$*" >>"$CALLS"
case "$*" in
--version) printf '%s\\n' '2.1.0 (Claude Code)' ;;
update)
    mkdir -p "$STUB_DIR/front"
    printf '#!/bin/bash\\nprintf "2.2.0 (Claude Code)\\\\n"\\n' >"$STUB_DIR/front/claude"
    chmod +x "$STUB_DIR/front/claude"
    ;;
esac
exit 0
"""


@unittest.skipUnless(sys.platform == "linux", "runs the Linux updater")
@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "the updater refuses root by design")
class TestInstallAgents(unittest.TestCase):
    """`install-agents.sh`, run whole against stub tools."""

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.dir / "home"
        self.home.mkdir()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.calls = self.dir / "calls.log"
        self.calls.touch()
        (self.dir / "status.json").write_text(
            '{"server":{"restart_needed":false},"update":{"restart_needed":false,"server_binary_stale":false}}',
            encoding="utf-8",
        )

    def stub(self, name: str, banner: str, where: Path | None = None) -> Path:
        directory = where or self.bin
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        text = STUB.replace("$BANNER", banner.replace("'", ""))
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        if where is not None:
            (self.bin / name).symlink_to(path)
        return path

    def run_script(self, *arguments: str, pane: bool = False) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.dir / 'front'}:{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "CALLS": str(self.calls),
            "STUB_DIR": str(self.dir),
            "LC_ALL": "C.UTF-8",
        }
        if pane:
            env["HERDR_PANE_ID"] = "test-pane"
        return subprocess.run(
            [str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )

    def called(self) -> list[str]:
        return [
            line
            for line in self.calls.read_text(encoding="utf-8").splitlines()
            if not line.endswith("--version") and line not in READS
        ]

    def report(self) -> dict[str, dict[str, Any]]:
        result = self.run_script("--report", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return {row["tool"]: row for row in json.loads(result.stdout)["tools"]}

    def test_upgrade_drives_only_the_tools_present(self) -> None:
        self.stub("claude", "2.1.0 (Claude Code)")
        self.stub("cswap", "cswap 0.27.1")
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["claude update", "cswap upgrade"])
        self.assertIn("codex: not installed, leaving it absent", result.stdout)

    def test_upgrade_reports_the_version_now_first_on_path(self) -> None:
        stub = self.bin / "claude"
        stub.write_text(RELOCATING_STUB, encoding="utf-8")
        stub.chmod(0o755)
        result = self.run_script("--upgrade", "--yes", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude 2.1.0 -> 2.2.0", result.stdout)

    def codex_in_npm(self) -> Path:
        node_modules = self.dir / "lib" / "node_modules"
        self.stub("codex", "codex-cli 0.149.1", where=node_modules / "@openai" / "codex" / "bin")
        (self.dir / "npm_root").write_text(str(node_modules), encoding="utf-8")
        return node_modules

    def test_files_outside_the_npm_global_root_are_not_claimed_by_that_npm(self) -> None:
        self.codex_in_npm()
        self.stub("npm", "npm 10.0.0")
        (self.dir / "npm_root").write_text(
            str(self.dir / "elsewhere" / "node_modules"), encoding="utf-8"
        )
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), [])
        self.assertIn("not under the global root of the npm on PATH", result.stdout)

    def test_an_npm_that_cannot_say_its_root_claims_nothing(self) -> None:
        self.codex_in_npm()
        self.stub("npm", "npm 10.0.0")
        (self.dir / "npm_root").unlink()
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(self.called(), [])
        self.assertIn("does not own it", result.stdout)

    def test_an_npm_install_is_upgraded_through_npm_for_that_one_package(self) -> None:
        self.codex_in_npm()
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["npm install -g @openai/codex@latest"])
        self.assertEqual(self.report()["codex"]["manager"], "npm")

    def test_a_dry_run_prints_the_manager_command_and_runs_nothing(self) -> None:
        self.codex_in_npm()
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("--upgrade", "--yes", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry run] npm install -g @openai/codex@latest", result.stdout)
        self.assertEqual(self.called(), [])

    def test_the_report_names_the_command_an_upgrade_will_run(self) -> None:
        self.codex_in_npm()
        self.stub("npm", "npm 10.0.0")
        notes = self.report()["codex"]["notes"]
        self.assertEqual(
            notes, ["installed through npm, --upgrade runs: npm install -g @openai/codex@latest"]
        )
        self.assertNotIn("which upgrades it", " ".join(notes))

    def test_an_unwritable_npm_directory_is_reported_with_its_command_and_not_run(self) -> None:
        node_modules = self.codex_in_npm()
        self.stub("npm", "npm 10.0.0")
        node_modules.chmod(0o555)
        self.addCleanup(node_modules.chmod, 0o755)
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), [])
        self.assertIn(
            "is not writable by this user, so run as its owner: npm install -g @openai/codex@latest",
            result.stdout,
        )
        self.assertNotIn("sudo", result.stdout)
        self.assertIn("--upgrade does not run it", " ".join(self.report()["codex"]["notes"]))

    def test_a_failing_manager_is_reported_and_the_other_tools_still_run(self) -> None:
        self.codex_in_npm()
        failing = self.bin / "npm"
        failing.write_text(
            '#!/bin/bash\nprintf "npm %s\\n" "$*" >>"$CALLS"\n[[ $1 == install ]] && exit 3\ncat "$STUB_DIR/npm_root" "$STUB_DIR/npm_prefix" 2>/dev/null\nexit 0\n',
            encoding="utf-8",
        )
        failing.chmod(0o755)
        self.stub("claude", "2.1.0 (Claude Code)")
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("codex: npm failed to upgrade it", result.stderr)
        self.assertIn("Failed: codex", result.stderr)
        self.assertEqual(self.called(), ["claude update", "npm install -g @openai/codex@latest"])

    def test_bun_is_upgraded_through_bun(self) -> None:
        self.stub("opencode", "1.18.18", where=self.home / ".bun" / "bin")
        self.stub("bun", "1.1.0")
        result = self.run_script("--upgrade", "--yes", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["bun install -g opencode-ai@latest"])

    def test_homebrew_formula_and_cask_are_upgraded_by_the_name_in_their_path(self) -> None:
        self.stub(
            "opencode",
            "1.0.0",
            where=self.dir / "linuxbrew" / "Cellar" / "opencode" / "1.0.0" / "bin",
        )
        self.stub(
            "claude",
            "2.0.0",
            where=self.dir / "opt" / "homebrew" / "Caskroom" / "claude-code" / "2.0.0",
        )
        self.stub("brew", "Homebrew 4.0.0")
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            sorted(self.called()), ["brew upgrade --cask claude-code", "brew upgrade opencode"]
        )

    def test_an_npm_global_under_a_mise_node_is_npms_and_not_mises(self) -> None:
        root = self.dir / "mise" / "installs" / "node" / "20.1.0" / "lib" / "node_modules"
        self.stub(
            "claude", "2.1.0 (Claude Code)", where=root / "@anthropic-ai" / "claude-code" / "bin"
        )
        (self.dir / "npm_root").write_text(str(root), encoding="utf-8")
        self.stub("npm", "npm 10.0.0")
        self.stub("mise", "mise 2024.1.1")
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["npm install -g @anthropic-ai/claude-code@latest"])
        self.assertEqual(self.report()["claude"]["manager"], "npm")

    def test_an_npm_global_under_a_keg_only_node_in_the_cellar_is_npms_and_not_brews(self) -> None:
        root = self.dir / "linuxbrew" / "Cellar" / "node@20" / "20.1.0" / "lib" / "node_modules"
        self.stub("codex", "codex-cli 0.149.1", where=root / "@openai" / "codex" / "bin")
        (self.dir / "npm_root").write_text(str(root), encoding="utf-8")
        self.stub("npm", "npm 10.0.0")
        self.stub("brew", "Homebrew 4.0.0")
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["npm install -g @openai/codex@latest"])

    def mise_layout(self, listed: bool = True) -> Path:
        """An npm-backend tool laid out as mise does, a node_modules inside its install directory, and mise's listing of it."""
        install = self.dir / "mise" / "installs" / "npm-opencode-ai" / "1.0.0"
        package = install / "lib" / "node_modules" / "opencode-ai" / "bin"
        self.stub("opencode", "1.0.0", where=package)
        other = self.dir / "mise" / "installs" / "aqua-sst-opencode" / "0.9.0"
        entries = {"aqua:sst/opencode": [{"install_path": str(other)}]}
        if listed:
            entries["npm:opencode-ai"] = [{"install_path": str(install)}]
        (self.dir / "mise_ls.json").write_text(json.dumps(entries), encoding="utf-8")
        self.stub("mise", "mise 2024.1.1")
        return install

    def test_mise_upgrades_the_key_its_listing_gives_for_the_install_path(self) -> None:
        self.mise_layout()
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["mise upgrade npm:opencode-ai"])
        self.assertEqual(self.report()["opencode"]["manager"], "mise")

    def test_a_mise_tool_the_listing_does_not_hold_is_reported_and_not_run(self) -> None:
        self.mise_layout(listed=False)
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("mise upgrade", " ".join(self.called()))
        self.assertIn("upgrade it with mise by hand", result.stdout)

    def test_a_tool_with_no_recorded_package_name_is_reported_not_guessed(self) -> None:
        self.stub("herdr", "herdr 0.9.0", where=self.dir / "lib" / "node_modules" / "herdr" / "bin")
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("--upgrade", "--yes", "herdr")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), [])
        self.assertIn("no npm package name is recorded for herdr", result.stdout)

    def plan(self, tool: str, path: str, manager: str) -> str:
        """Runs the script's own manager_plan, with the final call to main cut off so it can be sourced."""
        body = SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0]
        harness = self.dir / "plan.sh"
        harness.write_text(
            body
            + f'\nmanager_plan {tool} {path} {manager}\nprintf "%s|%s" "${{PLAN_CMD[*]}}" "$PLAN_WHY"\n',
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(harness)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_apt_is_left_to_upgrade_host_and_nix_is_reported_not_run(self) -> None:
        self.assertEqual(
            self.plan("codex", "/usr/bin/codex", "apt"),
            "|apt owns it and upgrade-host.sh upgrades it",
        )
        nix = self.plan("codex", "/nix/store/x/bin/codex", "nix")
        self.assertTrue(nix.startswith("|Nix is declarative, so update it where it is declared"))

    def test_install_names_what_it_installs(self) -> None:
        result = self.run_script("--install", "--yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nothing is installed by default", result.stderr)

    def test_install_leaves_an_installed_tool_alone(self) -> None:
        self.stub("herdr", "herdr 0.9.4")
        result = self.run_script("--install", "--yes", "herdr")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), [])

    def test_install_dry_run_names_the_installer(self) -> None:
        result = self.run_script("--install", "--dry-run", "claude", "cswap")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(
            result.stdout, r"\[dry run\] curl -fsSL https://claude.ai/install.sh -o \S+ && bash \S+"
        )
        self.assertIn("[dry run] uv tool install claude-swap", result.stdout)

    def test_herdr_upgrade_reinstalls_only_outdated_integrations(self) -> None:
        self.stub("herdr", "herdr 0.9.4")
        (self.dir / "outdated").write_text("claude: outdated (v9) (/x/hook.sh)\n", encoding="utf-8")
        result = self.run_script("--upgrade", "--yes", "herdr")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.called()
        self.assertIn("herdr update", calls)
        self.assertIn("herdr integration install claude", calls)
        self.assertEqual(
            [call for call in calls if call.startswith("herdr integration install")],
            ["herdr integration install claude"],
        )

    def test_herdr_is_not_updated_from_inside_its_own_pane(self) -> None:
        self.stub("herdr", "herdr 0.9.4")
        result = self.run_script("--upgrade", "--yes", "herdr", pane=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("herdr update", self.called())
        self.assertIn("inside a herdr pane", result.stdout)

    def test_restart_pending_reaches_the_report(self) -> None:
        jq = shutil.which("jq")
        if jq is None:
            self.skipTest("herdr's restart state is read with jq")
        (self.bin / "jq").symlink_to(jq)
        self.stub("herdr", "herdr 0.9.4")
        self.assertIs(self.report()["herdr"]["restart_pending"], False)
        status = {
            "server": {"restart_needed": True},
            "update": {"server_binary_stale": True, "restart_needed": True},
        }
        (self.dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        row = self.report()["herdr"]
        self.assertIs(row["restart_pending"], True)
        self.assertTrue(any("restart" in note for note in row["notes"]))
        for unreadable in (
            '{"server": {}}',
            '{"update": {"restart_needed": null}}',
            '{"update": {"restart_needed": "false"}}',
        ):
            with self.subTest(status=unreadable):
                (self.dir / "status.json").write_text(unreadable, encoding="utf-8")
                self.assertIsNone(self.report()["herdr"]["restart_pending"])
        text = self.run_script("--report")
        self.assertIn("could not read whether a running server needs a restart", text.stdout)

    def test_report_shape(self) -> None:
        self.stub("claude", "2.1.299 (Claude Code)")
        rows = self.report()
        self.assertEqual(list(rows), ["claude", "codex", "opencode", "herdr", "cswap"])
        for row in rows.values():
            self.assertEqual(
                set(row),
                {"tool", "installed", "path", "manager", "status", "restart_pending", "notes"},
            )
        self.assertEqual(rows["claude"]["installed"], "2.1.299")
        self.assertEqual(rows["claude"]["manager"], "self")
        self.assertEqual(
            rows["codex"],
            {
                "tool": "codex",
                "installed": None,
                "path": None,
                "manager": None,
                "status": "absent",
                "restart_pending": None,
                "notes": [],
            },
        )

    def test_refusals(self) -> None:
        for arguments, message in [
            (("--upgrade", "--json"), "applies only to --report"),
            (("--upgrade", "--all"), "--all names every tool for --install"),
            (("--report", "nosuch"), 'Unknown tool "nosuch"'),
            (("--report", "Herdr"), 'Unknown tool "Herdr"'),
            (("--report", "--upgrade"), "More than one action"),
        ]:
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_json_string_round_trips_every_escaped_character(self) -> None:
        lines = SCRIPT.read_text(encoding="utf-8").rstrip("\n").split("\n")
        self.assertEqual(lines[-1], 'main "$@"')
        functions = self.dir / "functions.sh"
        functions.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", "-c", f'source "{functions}"\njson_string "$1"\n', "bash", AWKWARD],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "LC_ALL": "C.UTF-8"},
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), AWKWARD)


PWSH = shutil.which("pwsh")
SCRIPT_PS = ROOT / "host-setup" / "windows" / "install-agents.ps1"


@unittest.skipUnless(PWSH, "needs pwsh to run the Windows updater")
# The stubs are bash scripts, and a hosted Windows runner is elevated, which the updater refuses by design.
# This harness therefore runs the Windows script under pwsh on a POSIX host.
@unittest.skipIf(sys.platform == "win32", "drives the Windows updater with POSIX stub tools")
class TestInstallAgentsWindows(unittest.TestCase):
    """`install-agents.ps1`, run whole under pwsh on this host against stub tools.

    The Windows-only checks degrade on another host, so the elevation refusal is not exercised
    here, and neither is a real Windows path. Manager detection reads a path with either separator.
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.dir / "home"
        self.home.mkdir()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.calls = self.dir / "calls.log"
        self.calls.touch()
        (self.dir / "status.json").write_text(
            '{"update":{"restart_needed":false}}', encoding="utf-8"
        )

    def stub(self, name: str, banner: str, directory: Path | None = None) -> Path:
        where = directory or self.bin
        where.mkdir(parents=True, exist_ok=True)
        path = where / name
        path.write_text(STUB.replace("$BANNER", banner.replace("'", "")), encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_script(
        self,
        *arguments: str,
        pane: bool = False,
        extra_path: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        directories = [str(self.bin), "/usr/bin", "/bin"]
        if extra_path is not None:
            directories.insert(0, str(extra_path))
        env = {
            "PATH": os.pathsep.join(directories),
            "HOME": str(self.home),
            "CALLS": str(self.calls),
            "STUB_DIR": str(self.dir),
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "POWERSHELL_TELEMETRY_OPTOUT": "1",
        }
        if pane:
            env["HERDR_PANE_ID"] = "test-pane"
        env.update(extra_env or {})
        assert PWSH is not None
        return subprocess.run(
            [PWSH, "-NoProfile", "-File", str(SCRIPT_PS), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
            timeout=120,
        )

    def recorded(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines()

    def test_upgrade_drives_only_present_tools(self) -> None:
        self.stub("claude", "2.1.0 (Claude Code)")
        self.stub("herdr", "herdr 0.9.0")
        result = self.run_script("-Upgrade", "-Yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertIn("claude update", calls)
        self.assertIn("herdr update", calls)
        self.assertFalse([c for c in calls if c.startswith(("codex", "opencode", "uv"))])
        self.assertIn("codex: not installed, leaving it absent", result.stdout)

    def test_cswap_upgrade_runs_uv_directly(self) -> None:
        self.stub("cswap", "cswap 0.1.0")
        self.stub("uv", "uv 0.5.0")
        result = self.run_script("-Upgrade", "-Yes", "cswap")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uv tool upgrade claude-swap", self.recorded())
        self.assertNotIn("cswap upgrade", self.recorded())

    def test_node_modules_path_reads_as_npm_and_is_upgraded_through_npm(self) -> None:
        npm_bin = self.dir / "node_modules" / ".bin"
        (self.dir / "npm_prefix").write_text(str(self.dir), encoding="utf-8")
        self.stub("codex", "codex-cli 0.1.0", npm_bin)
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=npm_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("npm install -g @openai/codex@latest", self.recorded())
        self.assertNotIn("codex update", self.recorded())

    def test_a_dry_run_prints_the_manager_command_and_runs_nothing(self) -> None:
        npm_bin = self.dir / "node_modules" / ".bin"
        (self.dir / "npm_prefix").write_text(str(self.dir), encoding="utf-8")
        self.stub("codex", "codex-cli 0.1.0", npm_bin)
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Upgrade", "-Yes", "-DryRun", "codex", extra_path=npm_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry run] npm install -g @openai/codex@latest", result.stdout)
        self.assertNotIn("npm install -g @openai/codex@latest", self.recorded())

    def test_the_report_names_the_command_and_not_the_false_claim(self) -> None:
        npm_bin = self.dir / "node_modules" / ".bin"
        (self.dir / "npm_prefix").write_text(str(self.dir), encoding="utf-8")
        self.stub("codex", "codex-cli 0.1.0", npm_bin)
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Report", "codex", extra_path=npm_bin)
        self.assertIn("-Upgrade runs: npm install -g @openai/codex@latest", result.stdout)
        self.assertNotIn("which upgrades it", result.stdout)

    def test_an_unwritable_npm_directory_is_reported_and_not_run(self) -> None:
        prefix = self.dir / "npmprefix"
        (self.dir / "npm_prefix").write_text(str(prefix), encoding="utf-8")
        self.stub("codex", "codex-cli 0.1.0", prefix / "node_modules" / ".bin")
        self.stub("npm", "npm 10.0.0")
        prefix.chmod(0o555)
        self.addCleanup(prefix.chmod, 0o755)
        result = self.run_script(
            "-Upgrade", "-Yes", "codex", extra_path=prefix / "node_modules" / ".bin"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is not writable by this user", result.stdout)
        self.assertNotIn("npm install -g @openai/codex@latest", self.recorded())

    def test_a_failing_manager_is_reported_and_the_other_tools_still_run(self) -> None:
        npm_bin = self.dir / "node_modules" / ".bin"
        (self.dir / "npm_prefix").write_text(str(self.dir), encoding="utf-8")
        self.stub("codex", "codex-cli 0.1.0", npm_bin)
        failing = self.bin / "npm"
        failing.write_text(
            '#!/bin/bash\nprintf "npm %s\\n" "$*" >>"$CALLS"\n[[ $1 == install ]] && exit 3\ncat "$STUB_DIR/npm_root" "$STUB_DIR/npm_prefix" 2>/dev/null\nexit 0\n',
            encoding="utf-8",
        )
        failing.chmod(0o755)
        self.stub("claude", "2.1.0 (Claude Code)")
        result = self.run_script("-Upgrade", "-Yes", extra_path=npm_bin)
        self.assertEqual(result.returncode, 1)
        self.assertIn("npm failed to upgrade it", result.stderr)
        self.assertIn("claude update", self.recorded())

    def test_bun_is_upgraded_through_bun(self) -> None:
        bun_bin = self.dir / ".bun" / "bin"
        self.stub("opencode", "1.0.0", bun_bin)
        self.stub("bun", "1.1.0")
        result = self.run_script("-Upgrade", "-Yes", "opencode", extra_path=bun_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bun install -g opencode-ai@latest", self.recorded())

    def test_scoop_updates_the_app_its_which_names(self) -> None:
        root = self.dir / "Apps" / "scoop"
        shims = root / "shims"
        self.stub("opencode", "1.0.0", shims)
        self.scoop_which(f"{root}/apps/opencode/current/opencode.exe")
        result = self.run_script(
            "-Upgrade", "-Yes", "opencode", extra_path=shims, extra_env={"SCOOP": str(root)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scoop update opencode", self.recorded())
        self.assertNotIn("scoop update scoop", self.recorded())

    def scoop_which(self, answer: str) -> None:
        scoop = self.bin / "scoop"
        scoop.write_text(
            '#!/bin/bash\nprintf "scoop %s\\n" "$*" >>"$CALLS"\n'
            f'[[ $1 == which ]] && echo "{answer}"\nexit 0\n',
            encoding="utf-8",
        )
        scoop.chmod(0o755)

    def test_scoops_tilde_path_for_a_per_user_install_is_expanded_before_matching(self) -> None:
        shims = self.home / "scoop" / "shims"
        self.stub("opencode", "1.0.0", shims)
        self.scoop_which("~\\scoop\\apps\\opencode\\current\\opencode.exe")
        result = self.run_script("-Upgrade", "-Yes", "opencode", extra_path=shims)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scoop update opencode", self.recorded())

    def test_a_scoop_path_outside_the_roots_apps_directory_is_reported_not_run(self) -> None:
        root = self.dir / "Apps" / "scoop"
        shims = root / "shims"
        self.stub("opencode", "1.0.0", shims)
        self.scoop_which(f"{self.dir}/Apps/other/apps/opencode/current/opencode.exe")
        result = self.run_script(
            "-Upgrade", "-Yes", "opencode", extra_path=shims, extra_env={"SCOOP": str(root)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("scoop update", " ".join(self.recorded()))
        self.assertIn("scoop's name for opencode could not be read", result.stdout)

    def test_shims_directly_in_the_npm_prefix_are_classified_as_npm(self) -> None:
        prefix = self.dir / "Program Files" / "nodejs"
        (self.dir / "npm_prefix").write_text(str(prefix), encoding="utf-8")
        self.stub("codex", "codex-cli 0.1.0", prefix)
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Report", "-Json", "codex", extra_path=prefix)
        row = json.loads(result.stdout)["tools"][0]
        self.assertEqual(row["manager"], "npm")

    def test_an_npm_global_under_a_scoop_node_is_npms_and_not_scoops(self) -> None:
        # Scoop's real layout: npm's prefix is the persist directory, while PATH lists the linked current\bin.
        prefix = self.dir / "scoop" / "persist" / "nodejs" / "bin"
        (self.dir / "npm_prefix").write_text(str(prefix), encoding="utf-8")
        self.stub("codex", "codex-cli 0.1.0", prefix)
        version = self.dir / "scoop" / "apps" / "nodejs" / "22.0.0"
        version.mkdir(parents=True)
        (version / "bin").symlink_to(prefix, target_is_directory=True)
        (version.parent / "current").symlink_to(version, target_is_directory=True)
        on_path = version.parent / "current" / "bin"
        self.stub("npm", "npm 10.0.0")
        self.scoop_which(f"{self.dir}/scoop/apps/nodejs/current/node.exe")
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=on_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("npm install -g @openai/codex@latest", " ".join(self.recorded()))
        self.assertNotIn("scoop update", " ".join(self.recorded()))

    def test_files_outside_the_npm_prefix_are_not_claimed_by_that_npm(self) -> None:
        (self.dir / "npm_prefix").write_text(str(self.dir / "somewhere"), encoding="utf-8")
        elsewhere = self.dir / "node_modules" / ".bin"
        self.stub("codex", "codex-cli 0.1.0", elsewhere)
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("npm install", " ".join(self.recorded()))
        self.assertIn("not under the prefix of the npm on PATH", result.stdout)

    def run_functions(self, body: str) -> subprocess.CompletedProcess[str]:
        """Runs the script's own helper functions, lifted by AST so main is not run."""
        assert PWSH is not None
        driver = (
            "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
            f"'{SCRIPT_PS}', [ref]$null, [ref]$null)\n"
            "foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {"
            " if ($f.Name -in 'Resolve-FinalPath', 'Test-ProtectedPath') { . ([scriptblock]::Create($f.Extent.Text)) } }\n"
            + body
        )
        return subprocess.run(
            [PWSH, "-NoProfile", "-Command", driver],
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=60,
        )  # fmt: skip

    def test_a_link_into_a_user_writable_directory_is_resolved_before_the_protected_rule(
        self,
    ) -> None:
        """A symlink stands in for the junction nvm-windows makes, since pwsh on Linux cannot create a junction.

        The Program Files rule itself is Windows-only, so this covers the resolution and the rule apart.
        """
        protected = self.dir / "Program Files"
        protected.mkdir()
        appdata = self.dir / "appdata" / "nvm"
        appdata.mkdir(parents=True)
        (protected / "nodejs").symlink_to(appdata, target_is_directory=True)
        body = (
            f"$link = '{protected / 'nodejs'}'\n"
            "$real = Resolve-FinalPath -Path $link\n"
            f"'resolved=' + ($real -eq '{appdata}')\n"
            f"'link-protected=' + (Test-ProtectedPath -Directory $link -Roots @('{protected}'))\n"
            f"'target-protected=' + (Test-ProtectedPath -Directory $real -Roots @('{protected}'))\n"
        )
        result = self.run_functions(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split(),
            ["resolved=True", "link-protected=True", "target-protected=False"],
        )

    def test_winget_is_left_to_upgrade_host(self) -> None:
        links = self.dir / "Microsoft" / "WinGet" / "Links"
        self.stub("opencode", "1.0.0", links)
        result = self.run_script("-Upgrade", "-Yes", "opencode", extra_path=links)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("winget owns it and upgrade-host upgrades it", result.stdout)
        self.assertEqual([c for c in self.recorded() if not c.endswith("--version")], [])

    def test_choco_is_reported_with_its_command_and_never_run(self) -> None:
        bin_dir = self.dir / "chocolatey" / "bin"
        self.stub("opencode", "1.0.0", bin_dir)
        self.stub("choco", "choco 2.0.0")
        result = self.run_script("-Upgrade", "-Yes", "opencode", extra_path=bin_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "choco needs an elevated shell and this script never elevates, so run in one: choco upgrade",
            result.stdout,
        )
        self.assertNotIn("choco upgrade", " ".join(self.recorded()))

    def test_install_without_names_is_refused(self) -> None:
        result = self.run_script("-Install", "-Yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("-Install names the tools it installs", result.stderr)
        self.assertEqual(self.recorded(), [])

    def test_refusals(self) -> None:
        for arguments, message in [
            (("-Report", "-Upgrade"), "More than one action"),
            (("-Upgrade", "-Json"), "applies only to -Report"),
            (("-Upgrade", "-All"), "-All names every tool for -Install"),
            (("-Install", "-All", "herdr"), "-All and a tool name were both given"),
            (("-Report", "nosuch"), 'Unknown tool "nosuch"'),
            (("-Report", "Herdr"), 'Unknown tool "Herdr"'),
        ]:
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_install_refuses_codex_and_opencode_without_a_windows_installer(self) -> None:
        result = self.run_script("-Install", "-Yes", "codex", "opencode")
        self.assertEqual(result.returncode, 1)
        self.assertIn("codex documents no Windows installer", result.stderr)
        self.assertIn("opencode documents no Windows installer", result.stderr)
        self.assertIn("Failed: codex opencode", result.stderr)

    def test_outdated_herdr_integrations_are_reinstalled(self) -> None:
        self.stub("herdr", "herdr 0.9.0")
        (self.dir / "outdated").write_text(
            "claude-code: outdated, run install\npi: outdated\n", encoding="utf-8"
        )
        result = self.run_script("-Upgrade", "-Yes", "herdr")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertIn("herdr integration install claude-code", calls)
        self.assertIn("herdr integration install pi", calls)

    def test_herdr_pane_skips_the_update_but_refreshes_integrations(self) -> None:
        self.stub("herdr", "herdr 0.9.0")
        (self.dir / "outdated").write_text("claude-code: outdated\n", encoding="utf-8")
        result = self.run_script("-Upgrade", "-Yes", "herdr", pane=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("inside a herdr pane", result.stdout)
        calls = self.recorded()
        self.assertNotIn("herdr update", calls)
        self.assertIn("herdr integration install claude-code", calls)

    def test_json_report_shape(self) -> None:
        home_bin = self.home / ".local" / "bin"
        self.stub("herdr", "herdr 0.9.4", home_bin)
        (self.dir / "status.json").write_text(
            '{"update":{"restart_needed":true}}', encoding="utf-8"
        )
        result = self.run_script("-Json", extra_path=home_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["platform"], "windows")
        self.assertEqual(report["notes"], [])
        tools = {entry["tool"]: entry for entry in report["tools"]}
        self.assertEqual(list(tools), ["claude", "codex", "opencode", "herdr", "cswap"])
        for entry in tools.values():
            self.assertEqual(
                set(entry),
                {"tool", "installed", "path", "manager", "status", "restart_pending", "notes"},
            )
        self.assertEqual(tools["codex"]["status"], "absent")
        self.assertIsNone(tools["codex"]["installed"])
        herdr = tools["herdr"]
        self.assertEqual(herdr["status"], "present")
        self.assertEqual(herdr["installed"], "0.9.4")
        self.assertEqual(herdr["manager"], "self")
        self.assertIs(herdr["restart_pending"], True)
        self.assertEqual(herdr["path"], "%USERPROFILE%/.local/bin/herdr")
        self.assertTrue(any("previous binary" in n for n in herdr["notes"]))

    def test_upgrade_reports_the_version_now_first_on_path(self) -> None:
        stub = self.bin / "claude"
        stub.write_text(RELOCATING_STUB, encoding="utf-8")
        stub.chmod(0o755)
        result = self.run_script("-Upgrade", "-Yes", "claude", extra_path=self.dir / "front")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude 2.1.0 -> 2.2.0", result.stdout)

    def test_restart_pending_is_null_when_unreadable(self) -> None:
        self.stub("herdr", "herdr 0.9.4")
        for unreadable in (
            '{"server": {}}',
            '{"update": {"restart_needed": null}}',
            '{"update": {"restart_needed": "false"}}',
        ):
            with self.subTest(status=unreadable):
                (self.dir / "status.json").write_text(unreadable, encoding="utf-8")
                result = self.run_script("-Json")
                self.assertEqual(result.returncode, 0, result.stderr)
                tools = {entry["tool"]: entry for entry in json.loads(result.stdout)["tools"]}
                self.assertIsNone(tools["herdr"]["restart_pending"])
        text = self.run_script("-Report")
        self.assertIn("could not read whether a running server needs a restart", text.stdout)

    def test_list_and_help_name_the_tools(self) -> None:
        listed = self.run_script("-List")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        for name in ("claude", "codex", "opencode", "herdr", "cswap"):
            self.assertIn(name, listed.stdout)
        helped = self.run_script("-Help")
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertTrue(helped.stdout.startswith("Usage: install-agents.ps1"))


if __name__ == "__main__":
    unittest.main()

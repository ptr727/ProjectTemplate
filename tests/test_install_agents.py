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
READS = {
    "npm root -g",
    "npm prefix -g",
    "mise ls --json",
    "mise which opencode",
    "mise which codex",
}

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
"which opencode"|"which codex") cat "$STUB_DIR/mise_which" 2>/dev/null || true ;;
"uninstall -g "*|"remove -g "*) [[ -z ${REMOVES:-} ]] || rm -f "$REMOVES" ;;
esac
exit 0
"""


# A curl that records the URL it was asked for and writes a no-op installer to the -o target.
# The script fetches each installer to a file before running it, so the target must exist.
CURL = """#!/bin/bash
printf 'curl %s\n' "${@: -3:1}" >>"$CALLS"
if [[ -f $STUB_DIR/installer.sh ]]; then
    cp "$STUB_DIR/installer.sh" "${@: -1}"
else
    printf '#!/bin/bash\nexit 0\n' >"${@: -1}"
fi
exit 0
"""

# An installer that lays the managed opencode down, so the check that it landed has something to find.
MANAGED_INSTALLER = """#!/bin/bash
mkdir -p "$HOME/.opencode/bin"
printf '#!/bin/bash\\nprintf "2.0.0\\\\n"\\n' >"$HOME/.opencode/bin/opencode"
chmod +x "$HOME/.opencode/bin/opencode"
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


RELOCATING_FAILING_STUB = RELOCATING_STUB.replace(
    "    ;;\nesac\nexit 0\n", "    exit 1\n    ;;\nesac\nexit 0\n"
)


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

    def run_script(
        self, *arguments: str, pane: bool = False, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.dir / 'front'}:{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "CALLS": str(self.calls),
            "STUB_DIR": str(self.dir),
            "LC_ALL": "C.UTF-8",
        }
        if pane:
            env["HERDR_PANE_ID"] = "test-pane"
        env.update(extra_env or {})
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

    def managed_opencode(self) -> Path:
        # Written without a link into self.bin, so only the managed path holds it and PATH does not reach it.
        path = self.home / ".opencode" / "bin" / "opencode"
        path.parent.mkdir(parents=True)
        path.write_text(STUB.replace("$BANNER", "2.0.0"), encoding="utf-8")
        path.chmod(0o755)
        return path

    def curl(self) -> None:
        path = self.bin / "curl"
        path.write_text(CURL, encoding="utf-8")
        path.chmod(0o755)

    def stray_npm_opencode(self) -> Path:
        node_modules = self.dir / "lib" / "node_modules"
        self.stub("opencode", "1.0.0", where=node_modules / "opencode-ai" / "bin")
        (self.dir / "npm_root").write_text(str(node_modules), encoding="utf-8")
        self.stub("npm", "npm 10.0.0")
        return self.bin / "opencode"

    def test_the_report_names_how_a_stray_opencode_is_consolidated(self) -> None:
        self.stray_npm_opencode()
        self.curl()
        notes = " ".join(self.report()["opencode"]["notes"])
        self.assertIn(
            "--upgrade removes it with npm uninstall -g opencode-ai, then installs the managed one",
            notes,
        )

    def test_upgrade_removes_a_stray_opencode_then_runs_the_managed_installer(self) -> None:
        self.managed_opencode()
        link = self.stray_npm_opencode()
        self.curl()
        result = self.run_script("--upgrade", "--yes", "opencode", extra_env={"REMOVES": str(link)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.called(), ["npm uninstall -g opencode-ai", "curl https://opencode.ai/v2/install"]
        )
        self.assertIn("opencode: removed the npm copy at", result.stdout)

    def test_a_stray_opencode_that_stays_is_upgraded_by_its_own_package_name(self) -> None:
        self.stray_npm_opencode()
        self.curl()
        # A global root this user cannot write makes the stray one only its owner can remove.
        node_modules = self.dir / "lib" / "node_modules"
        node_modules.chmod(0o555)
        self.addCleanup(node_modules.chmod, 0o755)
        result = self.run_script("--upgrade", "--yes", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("run as its owner: npm uninstall -g opencode-ai", result.stdout)
        self.assertIn("run as its owner: npm install -g opencode-ai@latest", result.stdout)
        self.assertEqual(self.called(), [])

    def test_a_dry_run_of_the_consolidation_prints_both_steps_and_runs_nothing(self) -> None:
        self.stray_npm_opencode()
        self.curl()
        result = self.run_script("--upgrade", "--yes", "--dry-run", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry run] npm uninstall -g opencode-ai", result.stdout)
        self.assertIn("https://opencode.ai/v2/install", result.stdout)
        self.assertEqual(self.called(), [])

    def test_upgrade_replaces_a_stray_opencode_with_a_managed_copy_that_lands(self) -> None:
        link = self.stray_npm_opencode()
        self.curl()
        (self.dir / "installer.sh").write_text(MANAGED_INSTALLER, encoding="utf-8")
        result = self.run_script("--upgrade", "--yes", "opencode", extra_env={"REMOVES": str(link)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.called(), ["npm uninstall -g opencode-ai", "curl https://opencode.ai/v2/install"]
        )
        self.assertIn("opencode 1.0.0 -> 2.0.0", result.stdout)

    def test_an_installer_that_leaves_no_managed_copy_fails_the_tool(self) -> None:
        link = self.stray_npm_opencode()
        self.curl()
        result = self.run_script("--install", "--yes", "opencode", extra_env={"REMOVES": str(link)})
        self.assertEqual(result.returncode, 3)
        self.assertIn("its installer finished, but no managed copy is at", result.stderr)

    def test_install_replaces_a_stray_opencode_when_no_managed_copy_is_in_place(self) -> None:
        self.stray_npm_opencode()
        self.curl()
        result = self.run_script("--install", "--yes", "--dry-run", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry run] npm uninstall -g opencode-ai", result.stdout)
        self.assertIn("https://opencode.ai/v2/install", result.stdout)
        self.assertNotIn("already installed", result.stdout)

    def test_a_copy_that_stays_behind_a_removed_one_is_upgraded_where_it_is(self) -> None:
        # A removable npm copy sits first on PATH, and a copy no manager owns sits behind it.
        node_modules = self.dir / "lib" / "node_modules"
        package = node_modules / "opencode-ai" / "bin"
        package.mkdir(parents=True)
        (package / "opencode").write_text(STUB.replace("$BANNER", "1.0.0"), encoding="utf-8")
        (package / "opencode").chmod(0o755)
        front = self.dir / "front"
        front.mkdir()
        (front / "opencode").symlink_to(package / "opencode")
        (self.dir / "npm_root").write_text(str(node_modules), encoding="utf-8")
        self.stub("npm", "npm 10.0.0")
        self.stub("opencode", "1.0.0")
        self.curl()
        result = self.run_script(
            "--upgrade", "--yes", "opencode", extra_env={"REMOVES": str(front / "opencode")}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["npm uninstall -g opencode-ai", "opencode upgrade"])

    def test_install_leaves_a_stray_opencode_ahead_of_the_managed_copy(self) -> None:
        self.managed_opencode()
        self.stray_npm_opencode()
        result = self.run_script("--install", "--yes", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--upgrade removes it, --install leaves it", result.stdout)
        self.assertEqual(self.called(), [])

    def test_a_stray_opencode_no_manager_owns_stays_and_runs_its_own_updater(self) -> None:
        self.stub("opencode", "1.0.0")
        self.curl()
        result = self.run_script("--upgrade", "--yes", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no package manager owns it, so delete it by hand", result.stdout)
        self.assertEqual(self.called(), ["opencode upgrade"])

    def test_the_managed_install_is_refused_where_curl_is_missing(self) -> None:
        lines = SCRIPT.read_text(encoding="utf-8").rstrip("\n").split("\n")
        functions = self.dir / "functions.sh"
        functions.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{functions}"\nPATH=/nonexistent managed_install_refusal opencode\n',
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "LC_ALL": "C.UTF-8"},
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("curl is not installed", result.stdout)

    def test_a_refused_managed_install_removes_nothing(self) -> None:
        # The refusal is stubbed rather than curl hidden, since a host curl in /usr/bin would answer.
        self.stray_npm_opencode()
        self.curl()
        lines = SCRIPT.read_text(encoding="utf-8").rstrip("\n").split("\n")
        functions = self.dir / "functions.sh"
        functions.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        script = (
            f'source "{functions}"\n'
            "managed_install_refusal() { printf 'refused for the test'; }\n"
            "ASSUME_YES=true\n"
            "upgrade_tool opencode\n"
        )
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "HOME": str(self.home),
                "CALLS": str(self.calls),
                "STUB_DIR": str(self.dir),
                "LC_ALL": "C.UTF-8",
            },
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cannot be installed: refused for the test", result.stdout)
        self.assertNotIn("npm uninstall", " ".join(self.called()))

    def test_a_removal_npm_cannot_resolve_names_the_package_in_the_path(self) -> None:
        # No npm on the test PATH, so only the node_modules segment names the package.
        if shutil.which("npm", path="/usr/bin:/bin"):
            self.skipTest("a host npm in /usr/bin or /bin is reachable")
        self.stub(
            "opencode", "1.0.0", where=self.dir / "lib" / "node_modules" / "opencode-ai" / "bin"
        )
        self.curl()
        result = self.run_script("--report")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("npm uninstall -g opencode-ai", result.stdout)
        self.assertNotIn("npm uninstall -g @opencode/cli", result.stdout)

    # The test PATH keeps /usr/bin and /bin for the script's own tools, so a host curl there would answer.
    @unittest.skipIf(
        shutil.which("curl", path="/usr/bin:/bin"), "a host curl in /usr/bin or /bin is reachable"
    )
    def test_nothing_is_removed_while_the_managed_copy_cannot_be_installed(self) -> None:
        # No curl stub, and the test PATH carries no curl, so the managed installer cannot be fetched.
        self.stray_npm_opencode()
        result = self.run_script("--upgrade", "--yes", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cannot be installed: curl is not installed", result.stdout)
        # The stray stays, so it is upgraded under the package name that owns it.
        self.assertEqual(self.called(), ["npm install -g opencode-ai@latest"])

    def test_a_managed_opencode_alone_is_upgraded_by_its_installer(self) -> None:
        self.stub("opencode", "2.0.0", where=self.home / ".opencode" / "bin")
        self.curl()
        result = self.run_script("--upgrade", "--yes", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["curl https://opencode.ai/v2/install"])

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

    def test_a_failed_upgrade_that_moved_the_version_is_still_listed_as_changed(self) -> None:
        stub = self.bin / "claude"
        stub.write_text(RELOCATING_FAILING_STUB, encoding="utf-8")
        stub.chmod(0o755)
        result = self.run_script("--upgrade", "--yes", "claude")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("claude: its updater failed", result.stderr)
        self.assertIn("Changed:\n  claude 2.1.0 -> 2.2.0", result.stdout)

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
        self.assertEqual(result.returncode, 3)
        self.assertIn("codex: npm failed to upgrade it", result.stderr)
        self.assertIn("Failed: codex", result.stderr)
        self.assertEqual(self.called(), ["claude update", "npm install -g @openai/codex@latest"])

    def test_bun_is_upgraded_through_bun(self) -> None:
        self.stub(
            "codex",
            "1.18.18",
            where=self.home
            / ".bun"
            / "install"
            / "global"
            / "node_modules"
            / "@openai"
            / "codex"
            / "bin",
        )
        self.stub("bun", "1.1.0")
        result = self.run_script("--upgrade", "--yes", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["bun install -g @openai/codex@latest"])

    def test_homebrew_formula_and_cask_are_upgraded_by_the_name_in_their_path(self) -> None:
        self.stub(
            "codex",
            "1.0.0",
            where=self.dir / "linuxbrew" / "Cellar" / "codex" / "1.0.0" / "bin",
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
            sorted(self.called()), ["brew upgrade --cask claude-code", "brew upgrade codex"]
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

    def test_a_fork_with_the_same_command_under_the_npm_root_is_reported_not_run(self) -> None:
        root = self.dir / "lib" / "node_modules"
        self.stub("codex", "codex-cli 9.9.9", where=root / "codex-fork" / "bin")
        (self.dir / "npm_root").write_text(str(root), encoding="utf-8")
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("--upgrade", "--yes", "codex")
        self.assertEqual(self.called(), [])
        self.assertIn("are not under the npm package @openai/codex", result.stdout)

    def test_a_fork_under_bun_is_reported_not_run(self) -> None:
        self.stub(
            "codex",
            "1.0.0",
            where=self.home / ".bun" / "install" / "global" / "node_modules" / "codex-fork" / "bin",
        )
        self.stub("bun", "1.1.0")
        result = self.run_script("--upgrade", "--yes", "codex")
        self.assertEqual(self.called(), [])
        self.assertIn("not under bun's global package @openai/codex", result.stdout)

    def test_an_agent_inside_a_homebrew_node_the_npm_does_not_own_is_reported_not_run(self) -> None:
        self.stub(
            "codex",
            "1.0.0",
            where=self.dir
            / "linuxbrew"
            / "Cellar"
            / "node@20"
            / "20.1.0"
            / "lib"
            / "node_modules"
            / "@openai"
            / "codex"
            / "bin",
        )
        self.stub("brew", "Homebrew 4.0.0")
        result = self.run_script("--upgrade", "--yes", "codex")
        self.assertEqual(self.called(), [])
        self.assertIn(
            "it sits inside Homebrew's Node, and the npm on PATH does not own it", result.stdout
        )

    def test_an_agent_inside_a_mise_node_the_npm_does_not_own_is_reported_not_run(self) -> None:
        install = self.dir / "mise" / "installs" / "node" / "20.1.0"
        self.stub(
            "codex", "1.0.0", where=install / "lib" / "node_modules" / "@openai" / "codex" / "bin"
        )
        (self.dir / "mise_ls.json").write_text(
            json.dumps({"core:node": [{"install_path": str(install)}]}), encoding="utf-8"
        )
        self.stub("mise", "mise 2024.1.1")
        result = self.run_script("--upgrade", "--yes", "codex")
        self.assertEqual(self.called(), [])
        self.assertIn(
            "it sits inside mise's Node, and the npm on PATH does not own it", result.stdout
        )

    def test_bun_honors_its_global_dir_variable_and_not_bunfig(self) -> None:
        custom = self.dir / "custom-global"
        self.stub("codex", "1.0.0", where=custom / "node_modules" / "@openai" / "codex" / "bin")
        self.stub("bun", "1.1.0")
        (self.home / "bunfig.toml").write_text(
            '[install]\nglobalDir = "/elsewhere"\n', encoding="utf-8"
        )
        result = self.run_script(
            "--upgrade", "--yes", "codex", extra_env={"BUN_INSTALL_GLOBAL_DIR": str(custom)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["bun install -g @openai/codex@latest"])

    def mise_shim(self, resolves: bool) -> None:
        """A mise shim, a symlink to the mise executable that Homebrew keeps in its Cellar."""
        cellar = self.dir / "linuxbrew" / "Cellar" / "mise" / "2024.1.1" / "bin"
        mise = self.stub("mise", "mise 2024.1.1", where=cellar)
        install = self.dir / "mise" / "installs" / "npm-openai-codex" / "1.0.0"
        real = install / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex"
        real.parent.mkdir(parents=True)
        real.write_text("#!/bin/bash\necho 1.0.0\n", encoding="utf-8")
        real.chmod(0o755)
        (self.bin / "codex").symlink_to(mise)
        (self.dir / "mise_ls.json").write_text(
            json.dumps({"npm:@openai/codex": [{"install_path": str(install)}]}), encoding="utf-8"
        )
        if resolves:
            (self.dir / "mise_which").write_text(str(real), encoding="utf-8")
        self.stub("brew", "Homebrew 4.0.0")

    def test_a_mise_shim_is_resolved_through_mise_which_and_never_upgrades_mise_itself(
        self,
    ) -> None:
        self.mise_shim(resolves=True)
        result = self.run_script("--upgrade", "--yes", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["mise upgrade npm:@openai/codex"])
        self.assertNotIn("brew", " ".join(self.called()))

    def test_a_mise_shim_mise_cannot_resolve_is_reported_and_nothing_runs(self) -> None:
        self.mise_shim(resolves=False)
        result = self.run_script("--upgrade", "--yes", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), [])
        self.assertIn("is a mise shim and mise which could not resolve it", result.stdout)

    def test_the_report_names_outdated_integrations_for_a_manager_owned_herdr(self) -> None:
        self.stub(
            "herdr",
            "herdr 0.9.4",
            where=self.dir / "linuxbrew" / "Cellar" / "herdr" / "0.9.4" / "bin",
        )
        self.stub("brew", "Homebrew 4.0.0")
        (self.dir / "outdated").write_text("claude: outdated (v9) (/x/hook.sh)\n", encoding="utf-8")
        notes = self.report()["herdr"]["notes"]
        self.assertIn("integrations outdated: claude, --upgrade reinstalls them", notes)
        self.assertTrue(any("--upgrade runs: brew upgrade herdr" in n for n in notes))

    def test_the_report_makes_no_post_step_promise_for_a_herdr_the_manager_will_not_run(
        self,
    ) -> None:
        self.stub("herdr", "herdr 0.9.4", where=self.dir / "lib" / "node_modules" / "herdr" / "bin")
        self.stub("npm", "npm 10.0.0")
        (self.dir / "outdated").write_text("claude: outdated (v9) (/x/hook.sh)\n", encoding="utf-8")
        notes = " ".join(self.report()["herdr"]["notes"])
        self.assertNotIn("integrations outdated", notes)

    def test_herdr_upgraded_by_a_manager_gets_the_same_post_step(self) -> None:
        self.stub(
            "herdr",
            "herdr 0.9.4",
            where=self.dir / "linuxbrew" / "Cellar" / "herdr" / "0.9.4" / "bin",
        )
        self.stub("brew", "Homebrew 4.0.0")
        (self.dir / "outdated").write_text("claude: outdated (v9) (/x/hook.sh)\n", encoding="utf-8")
        result = self.run_script("--upgrade", "--yes", "herdr")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.called()
        self.assertEqual(calls[0], "brew upgrade herdr")
        self.assertIn("herdr integration install claude", calls)

    def mise_layout(self, listed: bool = True) -> Path:
        """An npm-backend tool laid out as mise does, a node_modules inside its install directory, and mise's listing of it."""
        install = self.dir / "mise" / "installs" / "npm-openai-codex" / "1.0.0"
        package = install / "lib" / "node_modules" / "@openai" / "codex" / "bin"
        self.stub("codex", "1.0.0", where=package)
        other = self.dir / "mise" / "installs" / "aqua-openai-codex" / "0.9.0"
        entries = {"aqua:openai/codex": [{"install_path": str(other)}]}
        if listed:
            entries["npm:@openai/codex"] = [{"install_path": str(install)}]
        (self.dir / "mise_ls.json").write_text(json.dumps(entries), encoding="utf-8")
        self.stub("mise", "mise 2024.1.1")
        return install

    def test_mise_upgrades_the_key_its_listing_gives_for_the_install_path(self) -> None:
        self.mise_layout()
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["mise upgrade npm:@openai/codex"])
        self.assertEqual(self.report()["codex"]["manager"], "mise")

    def test_a_mise_tool_the_listing_does_not_hold_is_reported_and_not_run(self) -> None:
        self.mise_layout(listed=False)
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("mise upgrade", " ".join(self.called()))
        self.assertIn("upgrade it with mise by hand", result.stdout)

    def test_a_relocated_mise_tool_is_read_from_mises_listing_not_its_path(self) -> None:
        # MISE_DATA_DIR moved: no /mise/installs/ segment, and a plain binary that would otherwise read as self.
        install = self.dir / "agent-tools" / "installs" / "codex" / "1.0.0"
        self.stub("codex", "1.0.0", where=install / "bin")
        listing = {"codex": [{"install_path": str(install)}]}
        (self.dir / "mise_ls.json").write_text(json.dumps(listing), encoding="utf-8")
        self.stub("mise", "mise 2024.1.1")
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("--upgrade", "--yes", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["mise upgrade codex"])
        self.assertEqual(self.report()["codex"]["manager"], "mise")

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

    def classify(self, path: str, npm_root: str, env: dict[str, str] | None = None) -> str:
        """Runs the script's own tool_manager, with npm's global root fixed and main cut off so it can be sourced."""
        body = SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0]
        harness = self.dir / "classify.sh"
        harness.write_text(
            body + f"\nnpm_root() {{ printf '%s' '{npm_root}'; }}\ntool_manager '{path}'\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(harness)],
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=30,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.home), **(env or {})},
        )  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_bun_moved_by_bun_install_alone_is_buns_and_not_npms(self) -> None:
        bun_install = f"{self.dir}/bunhome"
        path = f"{bun_install}/install/global/node_modules/opencode-ai/bin/opencode"
        manager = self.classify(path, "/usr/lib/node_modules", {"BUN_INSTALL": bun_install})
        self.assertEqual(manager, "bun")

    def test_an_npm_root_under_a_bun_directory_is_npms_and_not_buns(self) -> None:
        root = "/opt/tools/.bun/npm/lib/node_modules"
        self.assertEqual(self.classify(f"{root}/@openai/codex/bin/codex.js", root), "npm")

    def test_a_bun_global_is_buns_when_npm_does_not_own_it(self) -> None:
        path = "/opt/tools/.bun/install/global/node_modules/opencode-ai/bin/opencode"
        self.assertEqual(self.classify(path, "/usr/lib/node_modules"), "bun")

    def test_a_nix_store_node_modules_is_nixs_and_not_npms(self) -> None:
        path = "/nix/store/abc-codex/lib/node_modules/@openai/codex/bin/codex.js"
        self.assertEqual(self.classify(path, "/usr/lib/node_modules"), "nix")

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

    def own_npm(self, shim: Path, prefix: Path, package: str) -> Path:
        """Installs `package` under the prefix and has the shim name its directory, as npm's own shim does."""
        directory = prefix / "node_modules" / package
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "package.json").write_text("{}", encoding="utf-8")
        with shim.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# node_modules/{package}/bin/cli.js\n")
        return shim

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

    def stray_npm_opencode(self, package: str) -> Path:
        npm_bin = self.dir / "node_modules" / ".bin"
        (self.dir / "npm_prefix").write_text(str(self.dir), encoding="utf-8")
        self.own_npm(self.stub("opencode", "1.0.0", npm_bin), self.dir, package)
        self.stub("npm", "npm 10.0.0")
        return npm_bin

    def test_the_report_names_how_a_stray_opencode_is_consolidated(self) -> None:
        npm_bin = self.stray_npm_opencode("opencode-ai")
        result = self.run_script("-Report", "-Json", "opencode", extra_path=npm_bin)
        notes = " ".join(json.loads(result.stdout)["tools"][0]["notes"])
        self.assertIn(
            "-Upgrade removes it with npm uninstall -g opencode-ai, then installs the npm package @opencode/cli",
            notes,
        )

    def test_a_dry_run_of_the_consolidation_prints_both_steps_and_runs_nothing(self) -> None:
        npm_bin = self.stray_npm_opencode("opencode-ai")
        result = self.run_script("-Upgrade", "-Yes", "-DryRun", "opencode", extra_path=npm_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry run] npm uninstall -g opencode-ai", result.stdout)
        self.assertIn("[dry run] npm install -g @opencode/cli", result.stdout)
        self.assertNotIn("npm uninstall", " ".join(self.recorded()))

    def test_a_managed_opencode_is_upgraded_through_npm(self) -> None:
        npm_bin = self.stray_npm_opencode("@opencode/cli")
        result = self.run_script("-Upgrade", "-Yes", "opencode", extra_path=npm_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("npm install -g @opencode/cli@latest", self.recorded())

    def npm_that_installs(self, shim: bool) -> None:
        """An npm that lays the managed package down on install, and its shim where `shim` is set."""
        steps = (
            '    mkdir -p "$STUB_DIR/node_modules/@opencode/cli"\n'
            '    printf "{}" >"$STUB_DIR/node_modules/@opencode/cli/package.json"\n'
        )
        if shim:
            steps += (
                "    printf '#!/bin/bash\\n# node_modules/@opencode/cli/bin/cli.js\\n'"
                ' >"$STUB_DIR/node_modules/.bin/opencode"\n'
                '    chmod +x "$STUB_DIR/node_modules/.bin/opencode"\n'
            )
        npm = self.bin / "npm"
        npm.write_text(
            STUB.replace("$BANNER", "npm 10.0.0").replace(
                "esac\nexit 0\n", '"install -g @opencode/cli")\n' + steps + "    ;;\nesac\nexit 0\n"
            ),
            encoding="utf-8",
        )

    def test_upgrade_removes_a_stray_opencode_then_installs_the_managed_package(self) -> None:
        npm_bin = self.stray_npm_opencode("opencode-ai")
        self.npm_that_installs(shim=True)
        result = self.run_script(
            "-Upgrade",
            "-Yes",
            "opencode",
            extra_path=npm_bin,
            extra_env={"REMOVES": str(npm_bin / "opencode")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [c for c in self.recorded() if c.startswith(("npm install", "npm uninstall"))]
        self.assertEqual(calls, ["npm uninstall -g opencode-ai", "npm install -g @opencode/cli"])

    def test_a_managed_package_installed_where_path_does_not_reach_fails_the_tool(self) -> None:
        npm_bin = self.stray_npm_opencode("opencode-ai")
        self.npm_that_installs(shim=False)
        result = self.run_script(
            "-Upgrade",
            "-Yes",
            "opencode",
            extra_path=npm_bin,
            extra_env={"REMOVES": str(npm_bin / "opencode")},
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("is installed, but PATH does not reach it", result.stderr)

    def test_install_replaces_a_stray_opencode_when_no_managed_package_is_in_place(self) -> None:
        npm_bin = self.stray_npm_opencode("opencode-ai")
        result = self.run_script("-Install", "-Yes", "-DryRun", "opencode", extra_path=npm_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry run] npm uninstall -g opencode-ai", result.stdout)
        self.assertIn("[dry run] npm install -g @opencode/cli", result.stdout)
        self.assertNotIn("already installed", result.stdout)

    def test_a_shim_outside_the_npm_prefix_is_not_planned_as_an_npm_removal(self) -> None:
        # A shim naming the package's directory, left by an earlier Node outside the prefix the npm on PATH reports.
        stale = self.dir / "old-node" / "npm"
        (self.dir / "npm_prefix").write_text(str(self.dir / "current"), encoding="utf-8")
        self.own_npm(self.stub("opencode", "1.0.0", stale), self.dir / "current", "opencode-ai")
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Upgrade", "-Yes", "opencode", extra_path=stale)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("npm uninstall", " ".join(self.recorded()))

    @unittest.skipIf(
        shutil.which("npm", path="/usr/bin:/bin"), "a host npm in /usr/bin or /bin is reachable"
    )
    def test_a_refused_install_names_no_removal_for_a_stray_outside_the_npm_on_path(self) -> None:
        # A shim left in an npm-shaped directory after Node was removed, with no npm to own it.
        left = self.dir / "npm"
        self.stub("opencode", "1.0.0", left)
        result = self.run_script("-Upgrade", "-Yes", "opencode", extra_path=left)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cannot be installed: npm is not installed", result.stdout)
        self.assertNotIn("needs this copy removed first", result.stdout)

    # Root writes any directory, so the unwritable prefix this test builds would not hold.
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write any prefix")
    def test_a_stray_opencode_that_stays_is_planned_by_its_own_package_name(self) -> None:
        npm_bin = self.stray_npm_opencode("opencode-ai")
        # A prefix this user cannot write leaves the managed package uninstallable, so the stray stays.
        self.dir.chmod(0o555)
        self.addCleanup(self.dir.chmod, 0o755)
        result = self.run_script("-Upgrade", "-Yes", "opencode", extra_path=npm_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("npm install -g opencode-ai@latest", result.stdout)
        self.assertIn("It needs this copy removed first", result.stdout)
        self.assertIn("npm uninstall -g opencode-ai", result.stdout)
        self.assertNotIn("npm uninstall", " ".join(self.recorded()))

    def test_a_managed_package_path_does_not_reach_fails_the_upgrade(self) -> None:
        npm_bin = self.stray_npm_opencode("opencode-ai")
        managed = self.dir / "node_modules" / "@opencode" / "cli"
        managed.mkdir(parents=True)
        (managed / "package.json").write_text("{}", encoding="utf-8")
        result = self.run_script(
            "-Upgrade",
            "-Yes",
            "opencode",
            extra_path=npm_bin,
            extra_env={"REMOVES": str(npm_bin / "opencode")},
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("is installed, but PATH does not reach it", result.stderr)

    # The test PATH keeps /usr/bin and /bin, so a host npm there would answer.
    @unittest.skipIf(
        shutil.which("npm", path="/usr/bin:/bin"), "a host npm in /usr/bin or /bin is reachable"
    )
    def test_a_stray_opencode_stays_and_runs_its_own_updater_while_npm_is_absent(self) -> None:
        self.stub("opencode", "1.0.0")
        result = self.run_script("-Upgrade", "-Yes", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cannot be installed: npm is not installed", result.stdout)
        self.assertIn("opencode upgrade", self.recorded())
        self.assertNotIn("npm uninstall", " ".join(self.recorded()))

    def test_a_stray_choco_opencode_stays_and_runs_no_choco(self) -> None:
        bin_dir = self.dir / "chocolatey" / "bin"
        self.stub("opencode", "1.0.0", bin_dir)
        self.stub("choco", "choco 2.0.0")
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Upgrade", "-Yes", "opencode", extra_path=bin_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("choco needs an elevated shell", result.stdout + result.stderr)
        self.assertEqual(
            [c for c in self.recorded() if c.startswith("choco") and not c.endswith("--version")],
            [],
        )

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
        self.own_npm(self.stub("codex", "codex-cli 0.1.0", npm_bin), self.dir, "@openai/codex")
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=npm_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("npm install -g @openai/codex@latest", self.recorded())
        self.assertNotIn("codex update", self.recorded())

    def test_a_dry_run_prints_the_manager_command_and_runs_nothing(self) -> None:
        npm_bin = self.dir / "node_modules" / ".bin"
        (self.dir / "npm_prefix").write_text(str(self.dir), encoding="utf-8")
        self.own_npm(self.stub("codex", "codex-cli 0.1.0", npm_bin), self.dir, "@openai/codex")
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Upgrade", "-Yes", "-DryRun", "codex", extra_path=npm_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry run] npm install -g @openai/codex@latest", result.stdout)
        self.assertNotIn("npm install -g @openai/codex@latest", self.recorded())

    def test_the_report_names_the_command_and_not_the_false_claim(self) -> None:
        npm_bin = self.dir / "node_modules" / ".bin"
        (self.dir / "npm_prefix").write_text(str(self.dir), encoding="utf-8")
        self.own_npm(self.stub("codex", "codex-cli 0.1.0", npm_bin), self.dir, "@openai/codex")
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Report", "codex", extra_path=npm_bin)
        self.assertIn("-Upgrade runs: npm install -g @openai/codex@latest", result.stdout)
        self.assertNotIn("which upgrades it", result.stdout)

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0, "root can write the unwritable directory"
    )
    def test_an_unwritable_npm_directory_is_reported_and_not_run(self) -> None:
        prefix = self.dir / "npmprefix"
        (self.dir / "npm_prefix").write_text(str(prefix), encoding="utf-8")
        self.own_npm(
            self.stub("codex", "codex-cli 0.1.0", prefix / "node_modules" / ".bin"),
            prefix,
            "@openai/codex",
        )
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
        self.own_npm(self.stub("codex", "codex-cli 0.1.0", npm_bin), self.dir, "@openai/codex")
        failing = self.bin / "npm"
        failing.write_text(
            '#!/bin/bash\nprintf "npm %s\\n" "$*" >>"$CALLS"\n[[ $1 == install ]] && exit 3\ncat "$STUB_DIR/npm_root" "$STUB_DIR/npm_prefix" 2>/dev/null\nexit 0\n',
            encoding="utf-8",
        )
        failing.chmod(0o755)
        self.stub("claude", "2.1.0 (Claude Code)")
        result = self.run_script("-Upgrade", "-Yes", extra_path=npm_bin)
        self.assertEqual(result.returncode, 3)
        self.assertIn("npm failed to upgrade it", result.stderr)
        self.assertIn("claude update", self.recorded())

    def bun_package(self, name: str) -> None:
        package = self.home / ".bun" / "install" / "global" / "node_modules" / name
        package.mkdir(parents=True)
        (package / "package.json").write_text('{"name": "' + name + '"}', encoding="utf-8")

    def test_bun_is_upgraded_through_bun(self) -> None:
        # The real Windows layout of bun: a copied shim in .bun\bin and the package under install\global.
        bun_bin = self.home / ".bun" / "bin"
        self.stub("codex", "1.0.0", bun_bin)
        self.bun_package("@openai/codex")
        self.stub("bun", "1.1.0")
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=bun_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bun install -g @openai/codex@latest", self.recorded())

    def test_scoop_updates_the_app_its_which_names(self) -> None:
        root = self.dir / "Apps" / "scoop"
        shims = root / "shims"
        self.stub("codex", "1.0.0", shims)
        self.scoop_which(f"{root}/apps/codex/current/codex.exe")
        result = self.run_script(
            "-Upgrade", "-Yes", "codex", extra_path=shims, extra_env={"SCOOP": str(root)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scoop update codex", self.recorded())
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
        self.stub("codex", "1.0.0", shims)
        self.scoop_which("~\\scoop\\apps\\codex\\current\\codex.exe")
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=shims)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scoop update codex", self.recorded())

    def test_a_fork_under_the_npm_prefix_is_reported_not_run(self) -> None:
        prefix = self.dir / "npmprefix"
        (self.dir / "npm_prefix").write_text(str(prefix), encoding="utf-8")
        self.stub("codex", "codex-cli 9.9.9", prefix)
        self.own_npm(prefix / "codex", prefix, "codex-fork")
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=prefix)
        self.assertNotIn("npm install", " ".join(self.recorded()))
        self.assertIn("are not the npm package @openai/codex", result.stdout)

    def test_a_fork_under_bun_is_reported_not_run(self) -> None:
        fork = self.home / ".bun" / "bin"
        self.stub("codex", "1.0.0", fork)
        self.bun_package("codex-fork")
        self.stub("bun", "1.1.0")
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=fork)
        self.assertNotIn("bun install", " ".join(self.recorded()))
        self.assertIn("not under bun's global package @openai/codex", result.stdout)

    def test_bun_honors_its_global_dir_variable_and_not_bunfig(self) -> None:
        custom = self.dir / "custom-global"
        bun_bin = custom / "node_modules" / "@openai" / "codex" / "bin"
        self.stub("codex", "1.0.0", bun_bin)
        self.stub("bun", "1.1.0")
        (self.home / "bunfig.toml").write_text(
            '[install]\nglobalDir = "/elsewhere"\n', encoding="utf-8"
        )
        result = self.run_script(
            "-Upgrade",
            "-Yes",
            "codex",
            extra_path=bun_bin,
            extra_env={"BUN_INSTALL_GLOBAL_DIR": str(custom)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bun install -g @openai/codex@latest", self.recorded())

    def test_a_relocated_bun_is_recognized_by_its_copied_shim_in_its_bin_directory(self) -> None:
        # The real Windows layout with both bun variables moved: a copied shim in BUN_INSTALL\bin, no .bun segment.
        bun_install = self.dir / "tools" / "bunhome"
        global_dir = self.dir / "tools" / "bunglobal"
        self.stub("codex", "1.0.0", bun_install / "bin")
        package = global_dir / "node_modules" / "@openai/codex"
        package.mkdir(parents=True)
        (package / "package.json").write_text('{"name": "@openai/codex"}', encoding="utf-8")
        self.stub("bun", "1.1.0")
        result = self.run_script(
            "-Upgrade",
            "-Yes",
            "codex",
            extra_path=bun_install / "bin",
            extra_env={"BUN_INSTALL": str(bun_install), "BUN_INSTALL_GLOBAL_DIR": str(global_dir)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bun install -g @openai/codex@latest", self.recorded())
        self.assertNotIn("codex upgrade", " ".join(self.recorded()))

    def test_the_report_names_outdated_integrations_for_a_scoop_owned_herdr(self) -> None:
        root = self.dir / "AgentTools"
        shims = root / "shims"
        self.stub("herdr", "herdr 0.9.4", shims)
        self.scoop_which(f"{root}/apps/herdr/current/herdr.exe")
        (self.dir / "outdated").write_text("claude: outdated (v9) (/x/hook.sh)\n", encoding="utf-8")
        result = self.run_script(
            "-Report", "-Json", "herdr", extra_path=shims, extra_env={"SCOOP": str(root)}
        )
        notes = json.loads(result.stdout)["tools"][0]["notes"]
        self.assertIn("integrations outdated: claude, -Upgrade reinstalls them", notes)

    def test_herdr_upgraded_by_scoop_gets_the_same_post_step(self) -> None:
        root = self.dir / "AgentTools"
        shims = root / "shims"
        self.stub("herdr", "herdr 0.9.4", shims)
        self.scoop_which(f"{root}/apps/herdr/current/herdr.exe")
        (self.dir / "outdated").write_text("claude: outdated (v9) (/x/hook.sh)\n", encoding="utf-8")
        result = self.run_script(
            "-Upgrade", "-Yes", "herdr", extra_path=shims, extra_env={"SCOOP": str(root)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertIn("scoop update herdr", calls)
        self.assertIn("herdr integration install claude", calls)

    def test_a_scoop_root_written_with_a_trailing_separator_still_matches(self) -> None:
        root = self.dir / "AgentTools"
        shims = root / "shims"
        self.stub("herdr", "herdr 0.9.4", shims)
        self.scoop_which(f"{root}/apps/herdr/current/herdr.exe")
        result = self.run_script(
            "-Upgrade", "-Yes", "herdr", extra_path=shims, extra_env={"SCOOP": f"{root}/"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scoop update herdr", self.recorded())

    def test_scoops_own_config_names_the_root_when_the_environment_does_not(self) -> None:
        root = self.dir / "AgentTools"
        shims = root / "shims"
        self.stub("codex", "1.0.0", shims)
        config = self.home / ".config" / "scoop"
        config.mkdir(parents=True)
        (config / "config.json").write_text(json.dumps({"root_path": str(root)}), encoding="utf-8")
        report = self.run_script("-Report", "-Json", "codex", extra_path=shims)
        self.assertEqual(json.loads(report.stdout)["tools"][0]["manager"], "scoop")
        self.scoop_which(f"{root}/apps/codex/current/codex.exe")
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=shims)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scoop update codex", self.recorded())

    def test_an_unreadable_scoop_config_falls_back_to_the_defaults(self) -> None:
        config = self.home / ".config" / "scoop"
        config.mkdir(parents=True)
        (config / "config.json").write_text("{ not json", encoding="utf-8")
        shims = self.home / "scoop" / "shims"
        self.stub("codex", "1.0.0", shims)
        self.scoop_which("~\\scoop\\apps\\codex\\current\\codex.exe")
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=shims)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scoop update codex", self.recorded())

    def test_a_global_scoop_app_is_reported_with_its_elevated_command(self) -> None:
        glob = self.dir / "GlobalScoop"
        shims = glob / "shims"
        self.stub("codex", "1.0.0", shims)
        self.scoop_which(f"{glob}/apps/codex/current/codex.exe")
        result = self.run_script(
            "-Upgrade", "-Yes", "codex", extra_path=shims, extra_env={"SCOOP_GLOBAL": str(glob)}
        )
        self.assertNotIn("scoop update", " ".join(self.recorded()))
        self.assertIn(
            "scoop's global apps need an elevated shell and this script never elevates, so run in one: scoop update codex --global",
            result.stdout,
        )

    def test_scoops_nodejs_app_is_never_updated_for_an_agent(self) -> None:
        root = self.dir / "AgentTools"
        shims = root / "shims"
        self.stub("codex", "1.0.0", shims)
        self.scoop_which(f"{root}/apps/nodejs/current/bin/codex.exe")
        result = self.run_script(
            "-Upgrade", "-Yes", "codex", extra_path=shims, extra_env={"SCOOP": str(root)}
        )
        self.assertNotIn("scoop update", " ".join(self.recorded()))
        self.assertIn("it sits inside scoop's Node", result.stdout)

    def test_a_configured_scoop_root_without_a_scoop_segment_is_still_scoop(self) -> None:
        root = self.dir / "AgentTools"
        shims = root / "shims"
        self.stub("codex", "1.0.0", shims)
        result = self.run_script(
            "-Report", "-Json", "codex", extra_path=shims, extra_env={"SCOOP": str(root)}
        )
        self.assertEqual(json.loads(result.stdout)["tools"][0]["manager"], "scoop")
        self.scoop_which(f"{root}/apps/codex/current/codex.exe")
        upgraded = self.run_script(
            "-Upgrade", "-Yes", "codex", extra_path=shims, extra_env={"SCOOP": str(root)}
        )
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        self.assertIn("scoop update codex", self.recorded())

    def test_a_scoop_path_outside_the_roots_apps_directory_is_reported_not_run(self) -> None:
        root = self.dir / "Apps" / "scoop"
        shims = root / "shims"
        self.stub("codex", "1.0.0", shims)
        self.scoop_which(f"{self.dir}/Apps/other/apps/codex/current/codex.exe")
        result = self.run_script(
            "-Upgrade", "-Yes", "codex", extra_path=shims, extra_env={"SCOOP": str(root)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("scoop update", " ".join(self.recorded()))
        self.assertIn("scoop's name for codex could not be read", result.stdout)

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
        self.own_npm(self.stub("codex", "codex-cli 0.1.0", prefix), prefix, "@openai/codex")
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

    def test_winget_is_left_to_upgrade_host(self) -> None:
        links = self.dir / "Microsoft" / "WinGet" / "Links"
        self.stub("codex", "1.0.0", links)
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=links)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("winget owns it and upgrade-host upgrades it", result.stdout)
        self.assertEqual([c for c in self.recorded() if not c.endswith("--version")], [])

    def test_choco_is_reported_with_its_command_and_never_run(self) -> None:
        bin_dir = self.dir / "chocolatey" / "bin"
        self.stub("codex", "1.0.0", bin_dir)
        self.stub("choco", "choco 2.0.0")
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=bin_dir)
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

    def test_install_puts_opencode_in_the_managed_npm_package(self) -> None:
        self.stub("npm", "npm 10.0.0")
        result = self.run_script("-Install", "-Yes", "-DryRun", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry run] npm install -g @opencode/cli", result.stdout)

    def test_install_runs_the_documented_codex_windows_installer(self) -> None:
        result = self.run_script("-Install", "-Yes", "-DryRun", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry run] download https://chatgpt.com/codex/install.ps1", result.stdout)

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

    def test_a_failed_upgrade_that_moved_the_version_is_still_listed_as_changed(self) -> None:
        stub = self.bin / "claude"
        stub.write_text(RELOCATING_FAILING_STUB, encoding="utf-8")
        stub.chmod(0o755)
        result = self.run_script("-Upgrade", "-Yes", "claude", extra_path=self.dir / "front")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("claude: its updater failed", result.stderr)
        self.assertIn("Changed:\n  claude 2.1.0 -> 2.2.0", result.stdout)

    def hold_cswap_launcher(self) -> int:
        """Runs a copy of bash as the cswap launcher and returns its pid, with uv stubbed beside it."""
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("needs bash to stand in for a running cswap")
        launcher = self.bin / "cswap"
        shutil.copy(os.path.realpath(bash), launcher)
        launcher.chmod(0o755)
        self.stub("uv", "uv 0.5.0")
        running = subprocess.Popen([str(launcher), "-c", "sleep 120; :"])
        self.addCleanup(running.wait, 10)
        self.addCleanup(running.kill)
        return running.pid

    def test_cswap_is_skipped_while_a_running_cswap_holds_its_launcher(self) -> None:
        pid = self.hold_cswap_launcher()
        result = self.run_script("-Upgrade", "-Yes", "cswap")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"a running cswap (process {pid}) holds its launcher", result.stdout)
        self.assertNotIn("uv tool upgrade claude-swap", self.recorded())

    def test_a_dry_run_also_skips_cswap_while_a_running_cswap_holds_its_launcher(self) -> None:
        pid = self.hold_cswap_launcher()
        result = self.run_script("-Upgrade", "-Yes", "-DryRun", "cswap")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"a running cswap (process {pid}) holds its launcher", result.stdout)
        self.assertNotIn("uv tool upgrade claude-swap", result.stdout)

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


@unittest.skipUnless(PWSH, "needs pwsh")
class TestInstallAgentsWindowsHelpers(unittest.TestCase):
    """`install-agents.ps1`'s pure path helpers, lifted by AST, so they also run on a native Windows runner.

    Unlike TestInstallAgentsWindows, they need no stub tool and no unelevated shell.
    So a hosted Windows runner exercises their separator, case, and root-boundary handling on real paths.
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def run_functions(
        self, body: str, script: Path = SCRIPT_PS
    ) -> subprocess.CompletedProcess[str]:
        """Runs a script's own helper functions, lifted by AST so main is not run."""
        assert PWSH is not None
        driver = (
            "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
            f"'{script}', [ref]$null, [ref]$null)\n"
            "foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {"
            " if ($f.Name -in 'Resolve-FinalPath', 'Test-PathUnder', 'Test-ProtectedPath', 'Hide-Home', 'Get-CswapLauncherHolder') { . ([scriptblock]::Create($f.Extent.Text)) } }\n"
            + body
        )
        return subprocess.run(
            [PWSH, "-NoProfile", "-Command", driver],
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=60,
        )  # fmt: skip

    def test_hide_home_shortens_only_paths_inside_the_profile(self) -> None:
        # $HOME is read-only in PowerShell, so the paths are built from the real profile and a sibling of it.
        body = (
            "'inside=' + (Hide-Home -Path (Join-Path $HOME '.bun'))\n"
            "$sibling = $HOME.TrimEnd('/', '\\') + '2/bin'\n"
            "'sibling-kept=' + ((Hide-Home -Path $sibling) -eq $sibling)\n"
        )
        result = self.run_functions(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        inside, sibling = result.stdout.split("\n")[:2]
        self.assertTrue(inside.startswith("inside=%USERPROFILE%"), inside)
        self.assertEqual(sibling, "sibling-kept=True")

    def test_a_cswap_launcher_holder_is_either_launcher_in_any_case_and_nothing_else(self) -> None:
        body = (
            f"$dir = '{self.dir}'\n"
            "function Get-Process {\n"
            "  [pscustomobject]@{ Id = 41; Path = (Join-Path $dir 'cswap.exe') }\n"
            "  [pscustomobject]@{ Id = 42; Path = (Join-Path $dir.ToUpperInvariant() 'CLAUDE-SWAP.EXE') }\n"
            "  [pscustomobject]@{ Id = 43; Path = (Join-Path $dir 'cswap-other.exe') }\n"
            "  [pscustomobject]@{ Id = 44; Path = $null }\n"
            "}\n"
            "(Get-CswapLauncherHolder (Join-Path $dir 'cswap.exe')) -join ','\n"
        )
        result = self.run_functions(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "41,42", result.stderr)

    def test_install_tools_hides_home_the_same_way_and_the_two_copies_are_identical(self) -> None:
        tools = ROOT / "host-setup" / "windows" / "install-tools.ps1"

        def function_text(path: Path) -> str:
            text = path.read_text(encoding="utf-8")
            start = text.index("function Hide-Home {")
            return text[start : text.index("\n}\n", start) + 3]

        self.assertEqual(function_text(tools), function_text(SCRIPT_PS))
        body = (
            "'inside=' + (Hide-Home -Path (Join-Path $HOME '.bun'))\n"
            "$sibling = $HOME.TrimEnd('/', '\\') + '2/bin'\n"
            "'sibling-kept=' + ((Hide-Home -Path $sibling) -eq $sibling)\n"
        )
        result = self.run_functions(body, script=tools)
        self.assertEqual(result.returncode, 0, result.stderr)
        inside, sibling = result.stdout.split("\n")[:2]
        self.assertTrue(inside.startswith("inside=%USERPROFILE%"), inside)
        self.assertEqual(sibling, "sibling-kept=True")

    def test_path_under_reads_the_root_and_its_children_but_not_a_sibling(self) -> None:
        root = self.dir / "Program Files"
        body = (
            f"'equal=' + (Test-PathUnder -Path '{root}' -Root '{root}')\n"
            f"'trailing=' + (Test-PathUnder -Path '{root}/' -Root '{root}\\')\n"
            f"'child=' + (Test-PathUnder -Path '{root / 'nodejs'}' -Root '{root}')\n"
            f"'case=' + (Test-PathUnder -Path '{str(root).upper()}/NODEJS' -Root '{root}')\n"
            f"'sibling=' + (Test-PathUnder -Path '{self.dir / 'Program FilesPortable'}' -Root '{root}')\n"
            f"'below-root=' + (Test-PathUnder -Path '{root}' -Root '{root}' -Below)\n"
            f"'below-child=' + (Test-PathUnder -Path '{root / 'nodejs'}' -Root '{root}' -Below)\n"
            "'empty-root=' + (Test-PathUnder -Path 'x' -Root '')\n"
        )
        result = self.run_functions(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split(),
            [
                "equal=True",
                "trailing=True",
                "child=True",
                "case=True",
                "sibling=False",
                "below-root=False",
                "below-child=True",
                "empty-root=False",
            ],
        )

    def test_a_protected_root_matches_only_at_a_path_component(self) -> None:
        root = self.dir / "Program Files"
        body = (
            f"'root=' + (Test-ProtectedPath -Directory '{root}' -Roots @('{root}'))\n"
            f"'child=' + (Test-ProtectedPath -Directory '{root / 'nodejs'}' -Roots @('{root}'))\n"
            f"'sibling=' + (Test-ProtectedPath -Directory '{self.dir / 'Program FilesPortable' / 'npm'}' -Roots @('{root}'))\n"
        )
        result = self.run_functions(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["root=True", "child=True", "sibling=False"])

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
        try:
            (protected / "nodejs").symlink_to(appdata, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"this host cannot create a directory symlink: {error}")
        body = (
            f"$link = '{protected / 'nodejs'}'\n"
            "$real = Resolve-FinalPath -Path $link\n"
            # Windows may spell the resolved target differently from the temp path Python made, such as in 8.3 form.
            # So the test asks only that it lands in the user-writable directory the link points at.
            "'resolved=' + (($real -replace '/', '\\').TrimEnd('\\') -like '*\\appdata\\nvm')\n"
            f"'link-protected=' + (Test-ProtectedPath -Directory $link -Roots @('{protected}'))\n"
            f"'target-protected=' + (Test-ProtectedPath -Directory $real -Roots @('{protected}'))\n"
        )
        result = self.run_functions(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split(),
            ["resolved=True", "link-protected=True", "target-protected=False"],
        )


if __name__ == "__main__":
    unittest.main()

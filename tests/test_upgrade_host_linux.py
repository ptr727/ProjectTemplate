#!/usr/bin/env python3
"""Tests for `upgrade-host.sh --packages` retrying a failed upgrade, driven through the whole script.

Every command that would change the host is a stub on `PATH`: `apt-get`, `sudo`, `snap`, and `systemctl`.
`sleep` is stubbed too, so the retry's wait is recorded rather than spent.
Each `apt-get` stub fails on the attempts its `FAIL_<subcommand>` variable lists, which stands in for a mirror caught mid-sync.

Run as `python3 tests/test_upgrade_host_linux.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPGRADER = ROOT / "host-setup" / "linux" / "upgrade-host.sh"

APT_GET_STUB = """#!/bin/bash
set -Eeuo pipefail
printf 'apt-get %s\\n' "$*" >>"$STUB_LOG"
sub=${1//-/_}
count_file="$STUB_DIR/count.$sub"
count=$(($(cat "$count_file" 2>/dev/null || printf 0) + 1))
printf '%s' "$count" >"$count_file"
fail_var="FAIL_$sub"
for attempt in ${!fail_var:-}; do
    if [[ $attempt == "$count" ]]; then
        printf 'E: Unable to fetch some archives, attempt %s\\n' "$count" >&2
        exit 100
    fi
done
"""

RECORDING_STUB = """#!/bin/bash
printf '%s %s\\n' "$(basename "$0")" "$*" >>"$STUB_LOG"
"""

SUDO_STUB = """#!/bin/bash
exec "$@"
"""

INACTIVE_STUB = """#!/bin/bash
exit 1
"""


@unittest.skipUnless(
    sys.platform.startswith("linux") and Path("/etc/os-release").is_file(),
    "runs the Linux upgrader",
)
class TestPackageUpgradeRetry(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="upgrade-host-"))
        self.addCleanup(shutil.rmtree, self.work)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.log = self.work / "calls.log"
        stubs = {
            "apt-get": APT_GET_STUB,
            "sleep": RECORDING_STUB,
            "snap": RECORDING_STUB,
            "sudo": SUDO_STUB,
            "systemctl": INACTIVE_STUB,
        }
        for name, body in stubs.items():
            stub = self.bin / name
            stub.write_text(body, encoding="utf-8")
            stub.chmod(0o755)

    def run_upgrade(
        self, *args: str, fail: dict[str, str], stdin: int | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update({f"FAIL_{sub}": attempts for sub, attempts in fail.items()})
        env.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
                "STUB_DIR": str(self.work),
                "STUB_LOG": str(self.log),
                "TMPDIR": str(self.work),
            }
        )
        return subprocess.run(
            ["bash", str(UPGRADER), "--packages", *args],
            env=env,
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [
            " ".join(line.split()[:2]) for line in self.log.read_text(encoding="utf-8").splitlines()
        ]

    def test_an_unattended_upgrade_that_fails_once_reads_the_lists_again_and_succeeds(self) -> None:
        result = self.run_upgrade("--yes", fail={"full_upgrade": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            [
                "apt-get update",
                "apt-get full-upgrade",
                "sleep 90",
                "apt-get update",
                "apt-get full-upgrade",
                "apt-get install",
                "apt-get autoremove",
                "apt-get autoclean",
            ],
        )
        self.assertIn("a mirror still syncing is the usual cause", result.stderr)

    def test_an_unattended_upgrade_that_fails_twice_stops_with_apt_s_status(self) -> None:
        result = self.run_upgrade("--yes", fail={"full_upgrade": "1 2"})
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertEqual(
            self.calls(),
            [
                "apt-get update",
                "apt-get full-upgrade",
                "sleep 90",
                "apt-get update",
                "apt-get full-upgrade",
            ],
        )

    def test_a_failed_list_read_before_the_retry_stops_the_run(self) -> None:
        result = self.run_upgrade("--yes", fail={"full_upgrade": "1", "update": "2"})
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertEqual(
            self.calls(), ["apt-get update", "apt-get full-upgrade", "sleep 90", "apt-get update"]
        )

    def test_an_unattended_list_read_that_fails_once_is_retried_alone(self) -> None:
        result = self.run_upgrade("--yes", fail={"update": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            [
                "apt-get update",
                "sleep 90",
                "apt-get update",
                "apt-get full-upgrade",
                "apt-get install",
                "apt-get autoremove",
                "apt-get autoclean",
            ],
        )

    def test_an_unattended_install_that_fails_once_reads_the_lists_again_and_succeeds(self) -> None:
        result = self.run_upgrade("--yes", fail={"install": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            [
                "apt-get update",
                "apt-get full-upgrade",
                "apt-get install",
                "sleep 90",
                "apt-get update",
                "apt-get install",
                "apt-get autoremove",
                "apt-get autoclean",
            ],
        )

    def test_an_attended_upgrade_is_not_retried(self) -> None:
        primary, secondary = os.openpty()
        self.addCleanup(os.close, primary)
        try:
            os.write(primary, b"y\n")
            result = self.run_upgrade(fail={"full_upgrade": "1"}, stdin=secondary)
        finally:
            os.close(secondary)
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertEqual(self.calls(), ["apt-get update", "apt-get full-upgrade"])


if __name__ == "__main__":
    unittest.main()

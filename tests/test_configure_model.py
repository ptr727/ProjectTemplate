#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's workflow-model read by running its own lines.

A native Windows jq ends each raw output line with a carriage return. The shell is lifted out
of the file, and a stand-in jq appends that carriage return, so an edit that stops stripping it
fails here with "Unknown workflow model" instead of passing on a reimplementation.
"""

from __future__ import annotations

import json
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_configure_archived import lift, require

MODEL_RESOLUTION = lift(r'(if \[ -z "\$model" \]; then\n.*?\nesac\n)(?=main_ruleset=)')


class WorkflowModelCase(unittest.TestCase):
    """With no --model, the registry's model selects the develop ruleset."""

    def run_region(
        self, tmp: str, entry: dict[str, object], crlf: bool
    ) -> subprocess.CompletedProcess[str]:
        bash = require("bash", "jq", "sed")
        registry = Path(tmp) / "repos.json"
        registry.write_text(json.dumps({"repos": [entry]}), encoding="utf-8")
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        if crlf:
            shim = bin_dir / "jq"
            shim.write_text(
                '#!/bin/sh\nPATH="$REAL_PATH" jq "$@" | sed \'s/$/\\r/\'\n', encoding="utf-8"
            )
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        options = lift(r"^(set -[A-Za-z]+ [a-z]+)$")
        script = (
            f'{options}\nexport REAL_PATH="$PATH"\n'
            f"{'PATH=' + shlex.quote(str(bin_dir)) + ':"$PATH"' if crlf else ':'}\n"
            f"registry={shlex.quote(str(registry))}\nname={shlex.quote(str(entry['name']))}\n"
            f"model=''\nscript_dir=/x\n{MODEL_RESOLUTION}echo \"$develop_ruleset\"\n"
        )
        return subprocess.run(
            [bash, "-c", script], capture_output=True, text=True, timeout=30, check=False
        )

    def test_a_crlf_jq_output_still_resolves_the_registry_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_region(tmp, {"name": "Fixture", "workflowModel": "operational"}, True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Unknown workflow model", result.stderr)
            self.assertEqual(result.stdout, "/x/operational/develop.json\n")

    def test_plain_lf_output_resolves_the_same(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_region(
                tmp, {"name": "Fixture", "workflowModel": "operational"}, False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "/x/operational/develop.json\n")


if __name__ == "__main__":
    unittest.main()

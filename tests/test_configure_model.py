#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's workflow-model read by running its own lines.

A native Windows jq ends each raw output line with a carriage return. The shell is lifted out
of the file, and a stand-in jq appends that carriage return, so an edit that stops stripping it
fails here with "Unknown workflow model" instead of passing on a reimplementation.
"""

from __future__ import annotations

import json
import shlex
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from test_configure_archived import lift, run_bash

MODEL_RESOLUTION = lift(r'(if \[ -z "\$model" \]; then\n.*?\nesac\n)(?=main_ruleset=)')


class WorkflowModelCase(unittest.TestCase):
    """With no --model, the registry's model selects the develop ruleset."""

    def run_region(self, tmp: str, registry_text: str, crlf: bool) -> tuple[int, str, str]:
        registry = Path(tmp) / "repos.json"
        registry.write_text(registry_text, encoding="utf-8")
        path_line = ":"
        if crlf:
            real_jq = shutil.which("jq")
            if real_jq is None:
                raise unittest.SkipTest("no jq on PATH, so the script's own lines cannot be run")
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            shim = bin_dir / "jq"
            if Path(real_jq).resolve() == shim.resolve():
                raise AssertionError("the stand-in would call itself")
            shim.write_text(
                f'#!/bin/sh\n{shlex.quote(real_jq)} "$@" | awk \'{{printf "%s\\r\\n", $0}}\'\n',
                encoding="utf-8",
            )
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
            path_line = f'PATH={shlex.quote(str(bin_dir))}:"$PATH"'
        script = (
            f"{path_line}\nregistry={shlex.quote(str(registry))}\nname=Fixture\n"
            f"model=''\nscript_dir=/x\n{MODEL_RESOLUTION}echo \"$develop_ruleset\"\n"
        )
        result = run_bash(script, "jq", "sed", "awk")
        return result.returncode, result.stdout, result.stderr

    def entry(self) -> str:
        return json.dumps({"repos": [{"name": "Fixture", "workflowModel": "operational"}]})

    def test_a_crlf_jq_output_still_resolves_the_registry_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_region(tmp, self.entry(), True)
            self.assertEqual(code, 0, err)
            self.assertNotIn("Unknown workflow model", err)
            self.assertEqual(out, "/x/operational/develop.json\n")

    def test_plain_lf_output_resolves_the_same(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_region(tmp, self.entry(), False)
            self.assertEqual(code, 0, err)
            self.assertEqual(out, "/x/operational/develop.json\n")

    def test_a_registry_that_will_not_parse_fails_with_the_guards_own_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_region(tmp, "not json", False)
            self.assertEqual(code, 1)
            self.assertIn("Failed to read workflowModel", err)


if __name__ == "__main__":
    unittest.main()

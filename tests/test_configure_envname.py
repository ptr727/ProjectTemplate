#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's environment-name encoding by running its own lines.

A native Windows jq ends each raw output line with a carriage return. The shell is lifted out
of the file, and a stand-in jq appends that carriage return, so an edit that stops stripping it
fails here with a CR in the encoded name instead of passing on a reimplementation.
"""

from __future__ import annotations

import shlex
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from test_configure_archived import lift, run_bash

JQR = lift(r"^(jqr\(\) \{.*?\}$)")
ENCODE = lift(r'^\s*(ename_uri="\$\((?:jqr -n|jq -rn) --arg s "\$ename" \'\$s\|@uri\'\)")$')


class EnvironmentNameCase(unittest.TestCase):
    """The encoded environment name is a clean URL path segment."""

    def run_region(self, tmp: str, crlf: bool) -> tuple[int, str, str]:
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
            f"{path_line}\n{JQR}\nename='prod env'\n{ENCODE}\nprintf '%s' \"$ename_uri\" | od -c\n"
        )
        result = run_bash(script, "jq", "sed", "awk", "od")
        return result.returncode, result.stdout, result.stderr

    def test_a_crlf_jq_output_leaves_no_cr_in_the_encoded_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_region(tmp, True)
            self.assertEqual(code, 0, err)
            self.assertNotIn("\\r", out)
            self.assertIn("p   r   o   d   %   2   0   e   n   v", out)

    def test_plain_lf_output_encodes_the_same(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_region(tmp, False)
            self.assertEqual(code, 0, err)
            self.assertIn("p   r   o   d   %   2   0   e   n   v", out)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's check_settings() against an unparseable live response.

The shell is lifted out of the file rather than restated here, so an edit that removes the
guard fails these tests instead of leaving a reimplementation to agree with itself. Only `gh`,
the boundary the function calls out to, is stubbed, and every case runs offline.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIGURE = ROOT / "repo-config" / "configure.sh"


def lift(pattern: str) -> str:
    """The one region of configure.sh matching `pattern`, or a failure naming what was missing."""
    text = CONFIGURE.read_text(encoding="utf-8")
    found = re.findall(pattern, text, re.MULTILINE | re.DOTALL)
    if len(found) != 1:
        raise AssertionError(
            f"expected one match in {CONFIGURE.name} for {pattern!r}, found {len(found)}"
        )
    return found[0]


OPTIONS = lift(r"^(set -[A-Za-z]+ [a-z]+)$")
NOTE = lift(r"^(note\(\) \{.*?\}\n)")
FAIL = lift(r"^(fail\(\) \{\n.*?\n\}\n)")
PASS = lift(r"^(pass\(\) \{.*?\}\n|pass\(\) \{\n.*?\n\}\n)")
ASSERT = lift(r"^(assert\(\) \{\n.*?\n\}\n)")
JQR = lift(r"^(jqr\(\) \{ jq -r .*?\}\n)")
JQ_HAS = lift(r"^(jq_has\(\) \{ jq -e .*?\}\n)")
CHECK_SETTINGS = lift(r"^(check_settings\(\) \{\n.*?\n\}\n)")


class LiveSettingsCase(unittest.TestCase):
    """A 2xx settings response that is not a JSON object reports one FAIL and does not abort."""

    def run_check(self, live: str) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash")
        if bash is None or shutil.which("jq") is None:
            raise unittest.SkipTest("no bash or jq on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(json.dumps({"delete_branch_on_merge": True}), encoding="utf-8")
            script = (
                f"{OPTIONS}\nFAILED=0\nrepo=o/r\nname=r\ndescription=''\nregistry=/nonexistent\n"
                f"settings_file={json.dumps(str(settings))}\n"
                f"gh() {{ printf '%s' {json.dumps(live)}; }}\n"
                f"{NOTE}{FAIL}{PASS}{ASSERT}{JQR}{JQ_HAS}{CHECK_SETTINGS}"
                "check_settings\necho REACHED_END\n"
            )
            return subprocess.run(
                [bash, "-c", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )

    def test_a_non_json_body_is_one_named_fail_and_the_run_continues(self) -> None:
        result = self.run_check("<html>proxy</html>")
        self.assertIn(
            "FAIL live repository settings for 'o/r' were not one JSON object", result.stdout
        )
        self.assertIn("REACHED_END", result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_non_object_body_is_a_named_fail(self) -> None:
        result = self.run_check("[]")
        self.assertIn("were not one JSON object", result.stdout)
        self.assertIn("REACHED_END", result.stdout)

    def test_a_stream_ending_in_an_object_is_a_named_fail(self) -> None:
        result = self.run_check("[] {}")
        self.assertIn("were not one JSON object", result.stdout)
        self.assertIn("REACHED_END", result.stdout)

    def test_an_object_body_passes_the_guard(self) -> None:
        result = self.run_check('{"delete_branch_on_merge": true, "private": false}')
        self.assertNotIn("were not one JSON object", result.stdout)
        self.assertIn("REACHED_END", result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

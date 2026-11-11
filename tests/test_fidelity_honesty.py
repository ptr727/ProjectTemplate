#!/usr/bin/env python3
"""Drive fidelity_pass against a faked gh, for how it reads each repository's copy."""

from __future__ import annotations

import base64
import pathlib
import sys
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "spec"))
import audit
import fidelity_honesty

SLUG = "owner/Fixture"
ENTRY = {
    "name": "Fixture",
    "url": f"https://github.com/{SLUG}",
    "status": "cataloged",
    "types": ["python"],
}
CANONICAL = "catalog/snippets/configs/.codecov.yml"
CODECOV = {
    "path": ".codecov.yml",
    "fidelity": "intent",
    "reference": CANONICAL,
    "appliesTo": ["python"],
}


class FidelityPassReadCase(unittest.TestCase):
    """fidelity_pass reads each repository's copy at the unit's declared path and nowhere else.

    The canonical's history is a git walk that refuses a shallow clone and fetches the hub's
    origin, so it is answered as empty. The copy then matches on the current canonical alone.
    """

    def test_a_copy_at_the_declared_path_is_read_and_compared(self) -> None:
        canonical = (audit.ROOT / CANONICAL).read_bytes()
        reads: list[str] = []

        def fake_gh(path: str, ok404: bool = False) -> Any:
            reads.append(path)
            if path == f"repos/{SLUG}/contents/.codecov.yml?ref=main":
                return {"encoding": "base64", "content": base64.b64encode(canonical).decode()}
            raise AssertionError(f"unexpected gh read: {path}")

        spec = {
            "registry": {"repos": [ENTRY], "defaults": {}},
            "files": {"baseline": [CODECOV]},
        }
        with (
            mock.patch.object(audit, "gh", fake_gh),
            mock.patch.object(audit, "git_file_history", lambda rel_path: []),
        ):
            spreads, _, _ = fidelity_honesty.fidelity_pass(spec)

        self.assertIn(f"repos/{SLUG}/contents/.codecov.yml?ref=main", reads)
        [(unit, spread)] = spreads
        self.assertEqual(reads, [f"repos/{SLUG}/contents/.codecov.yml?ref=main"])
        self.assertEqual(unit["path"], ".codecov.yml")
        self.assertIsNotNone(spread)
        self.assertEqual(spread["match"], ["Fixture"])
        self.assertEqual(spread["unavailable"], [])


if __name__ == "__main__":
    unittest.main()

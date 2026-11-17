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

    def test_a_unit_with_repo_owned_keys_is_never_a_promotion_candidate(self) -> None:
        canonical = (audit.ROOT / CANONICAL).read_bytes()

        def fake_gh(path: str, ok404: bool = False) -> Any:
            return {"encoding": "base64", "content": base64.b64encode(canonical).decode()}

        for owned, want in ((None, 1), (["coverage.status"], 0)):
            unit = {**CODECOV, **({"repoOwnedKeys": owned} if owned else {})}
            spec = {"registry": {"repos": [ENTRY], "defaults": {}}, "files": {"baseline": [unit]}}
            with (
                self.subTest(owned=owned),
                mock.patch.object(audit, "gh", fake_gh),
                mock.patch.object(audit, "git_file_history", lambda rel_path: []),
            ):
                _, promote, _ = fidelity_honesty.fidelity_pass(spec)
                self.assertEqual(len(promote), want)


class ManifestGapPassCase(unittest.TestCase):
    """manifest_gap_pass reads the ledger, so a path the fleet retired outright still names its carriers."""

    def test_a_retired_path_the_hub_no_longer_tracks_names_its_carrier(self) -> None:
        spec = {
            "registry": {"repos": [ENTRY], "defaults": {}},
            "files": {"baseline": [{"path": "kept.md"}]},
            "divergences": {"gaps": [{"path": "gone.md", "disposition": "retire", "reason": "r"}]},
        }

        def fake_gh(path: str, ok404: bool = False) -> Any:
            if path == f"repos/{SLUG}/branches/main":
                return {"name": "main"}
            raise AssertionError(f"unexpected gh read: {path}")

        with (
            mock.patch.object(audit, "gh", fake_gh),
            mock.patch.object(audit, "hub_repo_name", lambda: "ProjectTemplate"),
            mock.patch.object(audit, "hub_tracked", lambda rev=None: {"kept.md", "hosted.sh"}),
            mock.patch.object(
                audit, "repo_tree", lambda slug, br: {"kept.md", "gone.md", "hosted.sh", "own.md"}
            ),
        ):
            gaps, unreadable = fidelity_honesty.manifest_gap_pass(spec)

        self.assertEqual(gaps, {"gone.md": ["Fixture"], "hosted.sh": ["Fixture"]})
        self.assertEqual(unreadable, [])

    def test_main_spec_carries_the_ledger_retired_paths(self) -> None:
        """Without the ledger in the spec, every path retired outright drops out of the gap pass."""
        self.assertIn("CLAUDE.md", audit.retired_paths(fidelity_honesty.load_spec()))


if __name__ == "__main__":
    unittest.main()

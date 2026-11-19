#!/usr/bin/env python3
"""Drive fidelity_pass against a faked gh, for how it reads each repository's copy."""

from __future__ import annotations

import base64
import contextlib
import io
import pathlib
import sys
import unittest
from typing import Any, ClassVar
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

    def gaps_for(
        self, rows: list[dict[str, Any]]
    ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        spec = {
            "registry": {"repos": [ENTRY], "defaults": {}},
            "files": {"baseline": [{"path": "kept.md"}]},
            "divergences": {"gaps": rows},
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
            gaps, unreadable, kept = fidelity_honesty.manifest_gap_pass(spec)
        self.assertEqual(unreadable, [])
        return gaps, kept

    def test_a_retired_path_the_hub_no_longer_tracks_names_its_carrier(self) -> None:
        self.assertEqual(
            self.gaps_for([{"path": "gone.md", "disposition": "retire", "reason": "r"}]),
            ({"gone.md": ["Fixture"], "hosted.sh": ["Fixture"]}, {}),
        )

    def test_a_scoped_keep_moves_that_repository_to_the_kept_carriers(self) -> None:
        """The report agrees with the audit, which reports no copy a scoped keep row covers."""
        keep = {"path": "gone.md", "disposition": "accepted", "reason": "r", "repos": ["Fixture"]}
        self.assertEqual(
            self.gaps_for([{"path": "gone.md", "disposition": "retire", "reason": "r"}, keep]),
            ({"hosted.sh": ["Fixture"]}, {"gone.md": ["Fixture"]}),
        )


class ConsoleKeptCase(unittest.TestCase):
    """The console names a kept copy rather than reading as though no repository carries one."""

    def console(self, gaps: dict[str, list[str]], kept: dict[str, list[str]]) -> str:
        out = io.StringIO()
        with (
            mock.patch.object(fidelity_honesty, "load_spec", lambda: {"divergences": {}}),
            mock.patch.object(fidelity_honesty, "fidelity_pass", lambda spec: ([], [], [])),
            mock.patch.object(fidelity_honesty, "manifest_gap_pass", lambda spec: (gaps, [], kept)),
            contextlib.redirect_stdout(out),
        ):
            fidelity_honesty.main([])
        return out.getvalue()

    def test_a_kept_copy_is_named_and_the_none_line_is_not_printed(self) -> None:
        out = self.console({}, {"gone.md": ["Fixture"]})
        self.assertIn("KEPT gone.md :: Fixture", out)
        self.assertNotIn("no cataloged repo carries", out)

    def test_no_carrier_at_all_prints_the_none_line(self) -> None:
        self.assertIn("no cataloged repo carries", self.console({}, {}))


class ScopedKeepReportCase(unittest.TestCase):
    """A scoped keep row renders while a kept repo still carries the path, and reads resolved once none does."""

    KEEP: ClassVar[dict[str, Any]] = {
        "path": "gone.md",
        "disposition": "accepted",
        "reason": "kept on purpose",
        "repos": ["Fixture"],
    }

    def report(self, kept: dict[str, list[str]]) -> str:
        return fidelity_honesty.render_report([], [], {}, [], {"gaps": [self.KEEP]}, kept)

    def test_a_kept_copy_still_carried_is_a_burn_down_row(self) -> None:
        out = self.report({"gone.md": ["Fixture"]})
        self.assertIn("- **gone.md** - Fixture - kept on purpose", out)
        self.assertNotIn("## Resolved", out)

    def test_a_keep_no_repository_still_carries_reads_resolved(self) -> None:
        out = self.report({})
        self.assertIn("- **gone.md** - Fixture (accepted)", out)

    def test_a_kept_repository_with_an_unreadable_tree_stays_unverified(self) -> None:
        """An unread tree is not proof the copy left, so the keep stays on its row."""
        out = fidelity_honesty.render_report([], [], {}, ["Fixture"], {"gaps": [self.KEEP]}, {})
        self.assertIn("_(unavailable, unverified: Fixture)_", out)
        self.assertNotIn("## Resolved", out)

    def test_a_kept_repository_that_dropped_its_copy_reads_now_resolved(self) -> None:
        keep = {**self.KEEP, "repos": ["Fixture", "Other"]}
        out = fidelity_honesty.render_report(
            [], [], {}, [], {"gaps": [keep]}, {"gone.md": ["Fixture"]}
        )
        self.assertIn(
            "- **gone.md** - Fixture _(recorded Other now resolved)_ - kept on purpose", out
        )

    def test_a_disposition_row_with_a_non_string_repo_is_dropped_rather_than_raising(self) -> None:
        bad = {"path": "x.md", "repos": [{"name": "A"}], "disposition": "accepted", "reason": "r"}
        out = fidelity_honesty.render_report([], [], {}, [], {"dispositions": [bad]}, {})
        self.assertNotIn("x.md", out)

    def test_a_disposition_row_with_no_repos_is_not_read_as_resolved(self) -> None:
        empty = {"path": "x.md", "repos": [], "disposition": "accepted", "reason": "r"}
        spread = {"differs": ["A"], "stale": [], "absent": [], "match": [], "unavailable": []}
        spreads = [({"path": "x.md", "fidelity": "verbatim"}, spread)]
        out = fidelity_honesty.render_report(spreads, [], {}, [], {"dispositions": [empty]}, {})
        self.assertNotIn("## Resolved", out)

    def test_main_spec_carries_the_ledger_retired_paths(self) -> None:
        """Without the ledger in the spec, every path retired outright drops out of the gap pass."""
        self.assertEqual(
            fidelity_honesty.load_spec()["divergences"], fidelity_honesty.load_ledger()
        )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Exercise build_dist.py's regeneration and staleness detection against a crafted skills tree.

Run as `python3 scripts/tests/test_build_dist.py`, or under `python3 -m unittest discover -s scripts/tests`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_dist


class TreeCase(unittest.TestCase):
    """Redirects every module path onto a temp tree so a case never touches this repo's own."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.skills_src = self.tmp / ".agents" / "skills"
        self.dist_plugin = self.tmp / ".claude-plugin" / "fleet-skills"
        self.github_skills = self.tmp / ".github" / "skills"
        self.addCleanup(
            self._restore,
            build_dist.SKILLS_SRC,
            build_dist.DIST_PLUGIN,
            build_dist.PLUGIN_MANIFEST,
            build_dist.DIGEST_DIR,
            build_dist.GITHUB_SKILLS,
            build_dist.INCLUDE_ROOT,
        )
        build_dist.INCLUDE_ROOT = self.tmp
        build_dist.SKILLS_SRC = self.skills_src
        build_dist.DIST_PLUGIN = self.dist_plugin
        build_dist.PLUGIN_MANIFEST = self.dist_plugin / ".claude-plugin" / "plugin.json"
        build_dist.DIGEST_DIR = self.dist_plugin / ".source-digests"
        build_dist.GITHUB_SKILLS = self.github_skills

    def _restore(self, src, dist, manifest, stamp, github_skills, include_root) -> None:
        build_dist.INCLUDE_ROOT = include_root
        build_dist.SKILLS_SRC = src
        build_dist.DIST_PLUGIN = dist
        build_dist.PLUGIN_MANIFEST = manifest
        build_dist.DIGEST_DIR = stamp
        build_dist.GITHUB_SKILLS = github_skills

    def make_skill(self, name: str, body: str = "content") -> None:
        d = self.skills_src / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")


class RegenerateCase(TreeCase):
    """Regeneration and staleness detection over the generated trees."""

    def test_a_symlink_in_a_skill_directory_is_rejected_by_regenerate(self) -> None:
        """shutil.copytree() follows a symlink by default, which would silently pull content
        from outside .agents/skills/ into the generated, published plugin."""
        self.make_skill("foo")
        (self.skills_src / "foo" / "escape").symlink_to(self.tmp)
        with self.assertRaises(ValueError):
            build_dist.regenerate()

    def test_a_symlink_in_a_skill_directory_is_rejected_by_skill_digest(self) -> None:
        self.make_skill("foo")
        (self.skills_src / "foo" / "escape").symlink_to(self.tmp)
        with self.assertRaises(ValueError):
            build_dist.skill_digest("foo")

    def test_a_skill_directory_that_is_itself_a_symlink_is_rejected(self) -> None:
        """rglob("*") only yields paths inside the directory it walks, so a skill directory that
        is itself a symlink to another tree would otherwise walk straight into that tree without
        the walk ever seeing the root symlink node."""
        outside = self.tmp / "outside-the-repo"
        outside.mkdir()
        (outside / "SKILL.md").write_text("not tracked here", encoding="utf-8")
        self.skills_src.mkdir(parents=True, exist_ok=True)
        (self.skills_src / "foo").symlink_to(outside)
        with self.assertRaises(ValueError):
            build_dist.skill_digest("foo")

    def test_no_skills_still_produces_a_valid_manifest(self) -> None:
        names = build_dist.regenerate()
        self.assertEqual(names, [])
        manifest = json.loads(build_dist.PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["skills"], [])
        self.assertFalse(build_dist.is_stale())

    def test_a_skill_directory_without_skill_md_is_not_discovered(self) -> None:
        (self.skills_src / "half-written").mkdir(parents=True)
        (self.skills_src / "half-written" / "notes.txt").write_text("wip", encoding="utf-8")
        self.assertEqual(build_dist.skill_names(), [])

    def test_digest_stamp_is_lf_only(self) -> None:
        """Read as bytes, not text: a text-mode read applies universal-newline translation and
        would hide a stray CR. build_dist.py's explicit `newline="\\n"` is what keeps this LF on
        a Windows host too; a Linux-CI runner's platform default is also LF, so this assertion
        alone cannot tell "explicit" from "happened to match", only that the shape is right."""
        self.make_skill("foo")
        build_dist.regenerate()
        content = (build_dist.DIGEST_DIR / "foo").read_bytes()
        self.assertNotIn(b"\r", content)
        self.assertTrue(content.endswith(b"\n"))

    def test_regenerate_copies_skill_content_and_lists_it_in_the_manifest(self) -> None:
        self.make_skill("foo")
        self.make_skill("bar")
        names = build_dist.regenerate()
        self.assertEqual(names, ["bar", "foo"])
        manifest = json.loads(build_dist.PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["skills"], ["./skills/bar", "./skills/foo"])
        self.assertEqual(
            (self.dist_plugin / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8"),
            "content",
        )
        self.assertEqual(
            (self.github_skills / "foo" / "SKILL.md").read_text(encoding="utf-8"),
            "content",
        )
        self.assertFalse(build_dist.is_stale())

    def test_a_deleted_github_skill_reports_stale(self) -> None:
        self.make_skill("foo")
        build_dist.regenerate()
        import shutil

        shutil.rmtree(self.github_skills / "foo")
        self.assertTrue(build_dist.is_stale())

    def test_a_github_skill_edited_in_place_reports_stale(self) -> None:
        self.make_skill("foo")
        build_dist.regenerate()
        (self.github_skills / "foo" / "SKILL.md").write_text("tampered", encoding="utf-8")
        self.assertTrue(build_dist.is_stale())

    def test_an_orphaned_github_skill_reports_stale(self) -> None:
        self.make_skill("foo")
        build_dist.regenerate()
        orphan = self.github_skills / "orphan"
        orphan.mkdir()
        (orphan / "SKILL.md").write_text("stray", encoding="utf-8")
        self.assertTrue(build_dist.is_stale())

    def test_a_stray_file_in_github_skills_reports_stale(self) -> None:
        self.make_skill("foo")
        build_dist.regenerate()
        (self.github_skills / "README.md").write_text("stray", encoding="utf-8")
        self.assertTrue(build_dist.is_stale())

    def test_a_missing_empty_github_skills_tree_reports_stale(self) -> None:
        build_dist.regenerate()
        self.github_skills.rmdir()
        self.assertTrue(build_dist.is_stale())

    def test_a_deleted_manifest_reports_stale_even_with_a_current_digest_stamp(self) -> None:
        self.make_skill("foo")
        build_dist.regenerate()
        build_dist.PLUGIN_MANIFEST.unlink()
        self.assertTrue(build_dist.is_stale())

    def test_a_hand_edited_non_skills_manifest_field_reports_stale(self) -> None:
        """Only the "skills" field was compared before; a hand-edited description, author, or
        version slipped through undetected. The manifest is entirely deterministic from `names`,
        so comparing all of it is both correct and free."""
        self.make_skill("foo")
        build_dist.regenerate()
        manifest = json.loads(build_dist.PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        manifest["description"] = "hand-edited, not what build_dist.py would generate"
        build_dist.PLUGIN_MANIFEST.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertTrue(build_dist.is_stale())

    def test_a_deleted_generated_skill_reports_stale_even_with_a_current_digest_stamp(self) -> None:
        self.make_skill("foo")
        build_dist.regenerate()
        import shutil

        shutil.rmtree(self.dist_plugin / "skills" / "foo")
        self.assertTrue(build_dist.is_stale())

    def test_a_generated_file_edited_in_place_reports_stale(self) -> None:
        """The stamps, the manifest, and SKILL.md's mere presence can all stay untouched
        while a generated file's actual content is edited or a non-SKILL.md file (a bundled
        scripts/ or references/ file) is deleted. Only comparing the generated tree's own digest
        against the source's catches this, since nothing else here re-reads the generated bytes."""
        self.make_skill("foo")
        (self.skills_src / "foo" / "references").mkdir()
        (self.skills_src / "foo" / "references" / "notes.md").write_text("v1", encoding="utf-8")
        build_dist.regenerate()
        (self.dist_plugin / "skills" / "foo" / "references" / "notes.md").write_text(
            "tampered", encoding="utf-8"
        )
        self.assertTrue(build_dist.is_stale())

    def test_a_generated_extra_file_deleted_reports_stale(self) -> None:
        self.make_skill("foo")
        (self.skills_src / "foo" / "references").mkdir()
        (self.skills_src / "foo" / "references" / "notes.md").write_text("v1", encoding="utf-8")
        build_dist.regenerate()
        (self.dist_plugin / "skills" / "foo" / "references" / "notes.md").unlink()
        self.assertTrue(build_dist.is_stale())

    def test_an_orphaned_generated_skill_directory_reports_stale(self) -> None:
        """tree_digest() only hashes the expected `names`, so an extra directory under
        DIST_PLUGIN/skills/ (a retired skill left behind, one added by hand) would never be
        read and could not affect a pure digest comparison. Checked by name explicitly."""
        self.make_skill("foo")
        build_dist.regenerate()
        (self.dist_plugin / "skills" / "orphan").mkdir()
        (self.dist_plugin / "skills" / "orphan" / "SKILL.md").write_text("stray", encoding="utf-8")
        self.assertTrue(build_dist.is_stale())

    def test_a_stray_file_in_generated_plugin_skills_reports_stale(self) -> None:
        self.make_skill("foo")
        build_dist.regenerate()
        (self.dist_plugin / "skills" / "README.md").write_text("stray", encoding="utf-8")
        self.assertTrue(build_dist.is_stale())

    def test_editing_a_skill_after_regenerate_reports_stale(self) -> None:
        self.make_skill("foo")
        build_dist.regenerate()
        (self.skills_src / "foo" / "SKILL.md").write_text("changed", encoding="utf-8")
        self.assertTrue(build_dist.is_stale())

    def test_adding_a_skill_after_regenerate_reports_stale(self) -> None:
        self.make_skill("foo")
        build_dist.regenerate()
        self.make_skill("bar")
        self.assertTrue(build_dist.is_stale())

    def test_a_missing_dist_tree_is_stale(self) -> None:
        self.make_skill("foo")
        self.assertTrue(build_dist.is_stale())

    def stamps(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in build_dist.DIGEST_DIR.iterdir()}

    def test_two_skill_edits_change_two_disjoint_stamp_files(self) -> None:
        """One stamp over every skill's bytes made any two concurrent skill edits conflict on it,
        so the stamps are one file per skill and an edit to one skill moves only that skill's.
        Two branches from one base, each editing a different skill, are played out in turn."""
        self.make_skill("foo", "foo v1")
        self.make_skill("bar", "bar v1")
        build_dist.regenerate()
        base = self.stamps()
        self.assertEqual(set(base), {"foo", "bar"})
        self.make_skill("foo", "foo v2")
        build_dist.regenerate()
        moved_by_foo = {name for name, value in self.stamps().items() if base[name] != value}
        self.make_skill("foo", "foo v1")
        self.make_skill("bar", "bar v2")
        build_dist.regenerate()
        moved_by_bar = {name for name, value in self.stamps().items() if base[name] != value}
        self.assertEqual(moved_by_foo, {"foo"})
        self.assertEqual(moved_by_bar, {"bar"})
        self.assertFalse(moved_by_foo & moved_by_bar)

    def test_a_hand_edited_stamp_reports_stale(self) -> None:
        self.make_skill("foo")
        build_dist.regenerate()
        (build_dist.DIGEST_DIR / "foo").write_text("0000000000000000\n", encoding="utf-8")
        self.assertTrue(build_dist.is_stale())

    def test_a_missing_stamp_reports_stale(self) -> None:
        self.make_skill("foo")
        build_dist.regenerate()
        (build_dist.DIGEST_DIR / "foo").unlink()
        self.assertTrue(build_dist.is_stale())

    def test_a_stamp_replaced_by_a_symlink_reports_stale(self) -> None:
        """is_file() follows a symlink, so a stamp pointing outside the tree would otherwise
        read as current whenever its target happens to hold the right digest."""
        self.make_skill("foo")
        build_dist.regenerate()
        stamp = build_dist.DIGEST_DIR / "foo"
        target = self.tmp / "outside-stamp"
        target.write_bytes(stamp.read_bytes())
        stamp.unlink()
        stamp.symlink_to(target)
        self.assertTrue(build_dist.is_stale())

    def test_an_orphaned_stamp_reports_stale(self) -> None:
        """A stamp for a retired skill is never read by the per-skill comparison, so it is
        caught by name, the way an orphaned generated directory is."""
        self.make_skill("foo")
        build_dist.regenerate()
        (build_dist.DIGEST_DIR / "retired").write_text("0000000000000000\n", encoding="utf-8")
        self.assertTrue(build_dist.is_stale())
        build_dist.regenerate()
        self.assertFalse(build_dist.is_stale())
        self.assertFalse((build_dist.DIGEST_DIR / "retired").exists())

    def test_digest_uses_forward_slashes_regardless_of_platform(self) -> None:
        """A digest built from str(Path) would use backslashes on Windows and disagree with a
        Linux machine over identical bytes. Reproduce the expected posix-joined hash by hand and
        confirm it matches, which would fail if skill_digest regressed to str(Path)."""
        self.make_skill("foo")
        (self.skills_src / "foo" / "scripts").mkdir()
        (self.skills_src / "foo" / "scripts" / "run.py").write_text("x", encoding="utf-8")

        import hashlib

        expected = hashlib.sha256()
        expected.update(b"foo")
        expected.update(b"foo/SKILL.md")
        expected.update(b"content")
        expected.update(b"foo/scripts/run.py")
        expected.update(b"x")
        self.assertEqual(build_dist.skill_digest("foo"), expected.hexdigest()[:16])

    def test_removing_a_skill_after_regenerate_no_longer_carries_it(self) -> None:
        self.make_skill("foo")
        self.make_skill("bar")
        build_dist.regenerate()
        import shutil

        shutil.rmtree(self.skills_src / "bar")
        names = build_dist.regenerate()
        self.assertEqual(names, ["foo"])
        self.assertFalse((self.dist_plugin / "skills" / "bar").exists())

    def test_main_reports_a_symlink_cleanly_instead_of_a_raw_traceback(self) -> None:
        self.make_skill("foo")
        (self.skills_src / "foo" / "escape").symlink_to(self.tmp)
        from unittest import mock

        with mock.patch("sys.argv", ["build_dist.py"]), mock.patch("builtins.print"):
            exit_code = build_dist.main()
        self.assertEqual(exit_code, 1)

    def test_check_reports_a_symlink_as_2_not_1(self) -> None:
        """1 is --check's own documented "stale" result, so a caller reading the exit code (host-setup/menu.sh among them) needs a different code to tell a real failure apart from that finding.

        is_stale() short-circuits to True (stale, exit 1) the moment the distribution stamp is
        missing, so the symlink has to be introduced only after a clean regenerate() already
        produced one, reaching the digest walk that actually raises rather than the early return.
        """
        self.make_skill("foo")
        build_dist.regenerate()
        (self.skills_src / "foo" / "escape").symlink_to(self.tmp)
        from unittest import mock

        with mock.patch("sys.argv", ["build_dist.py", "--check"]), mock.patch("builtins.print"):
            exit_code = build_dist.main()
        self.assertEqual(exit_code, 2)

    def test_check_reports_an_os_error_as_2_not_1(self) -> None:
        """is_stale() reads several files beyond the one already wrapped in its own try/except, and a permissions problem or a file removed out from under it raises OSError there, not ValueError."""
        from unittest import mock

        with (
            mock.patch("sys.argv", ["build_dist.py", "--check"]),
            mock.patch("builtins.print"),
            mock.patch.object(build_dist, "is_stale", side_effect=OSError("permission denied")),
        ):
            exit_code = build_dist.main()
        self.assertEqual(exit_code, 2)

    def test_check_reports_an_unreadable_manifest_as_2_not_1(self) -> None:
        """The manifest read has its own try/except inside is_stale() (JSONDecodeError, a genuinely stale manifest), and an OSError there has to propagate through it rather than being caught by the same clause, or an unreadable file reads as the ordinary stale result this test would otherwise miss."""
        if os.name != "posix":
            self.skipTest(
                "chmod does not carry POSIX unreadable-file semantics, and os.geteuid() does not exist, on this platform"
            )
        if os.geteuid() == 0:
            self.skipTest("running as root ignores the permission bits this test depends on")
        self.make_skill("foo")
        build_dist.regenerate()
        build_dist.PLUGIN_MANIFEST.chmod(0o000)
        self.addCleanup(build_dist.PLUGIN_MANIFEST.chmod, 0o644)
        from unittest import mock

        with mock.patch("sys.argv", ["build_dist.py", "--check"]), mock.patch("builtins.print"):
            exit_code = build_dist.main()
        self.assertEqual(exit_code, 2)


class IncludeCase(TreeCase):
    """Include regions: filled from a heading's body in the authored tree, and held to it by --check."""

    HOME = "# Rules\n\n## Alpha\n\nAlpha rule.\n\n- One\n- Two\n\n## Beta\n\nBeta rule.\n"

    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "RULES.md").write_text(self.HOME, encoding="utf-8")

    def region(self, key: str, body: str = "") -> str:
        return f"<!-- include: {key} -->\n{body}<!-- /include -->\n"

    def skill_text(self, name: str = "foo") -> str:
        return (self.skills_src / name / "SKILL.md").read_text(encoding="utf-8")

    def test_regenerate_fills_a_region_from_its_source_heading(self) -> None:
        """The region holds the heading's body, not its heading line, with one blank line each side."""
        self.make_skill(
            "foo", "# Foo\n\n## Scope\n\n" + self.region("RULES.md > Alpha") + "\nTail.\n"
        )
        build_dist.regenerate()
        self.assertEqual(
            self.skill_text(),
            "# Foo\n\n## Scope\n\n<!-- include: RULES.md > Alpha -->\n\nAlpha rule.\n\n- One\n- Two\n\n"
            "<!-- /include -->\n\nTail.\n",
        )
        self.assertFalse(build_dist.is_stale())

    def test_a_filled_region_reaches_both_generated_trees(self) -> None:
        self.make_skill("foo", self.region("RULES.md > Beta"))
        build_dist.regenerate()
        for tree in (self.dist_plugin / "skills", self.github_skills):
            self.assertIn("Beta rule.", (tree / "foo" / "SKILL.md").read_text(encoding="utf-8"))

    def test_regenerate_is_idempotent(self) -> None:
        self.make_skill("foo", self.region("RULES.md > Alpha"))
        build_dist.regenerate()
        once = self.skill_text()
        build_dist.regenerate()
        self.assertEqual(self.skill_text(), once)

    def test_a_file_without_a_region_is_never_rewritten(self) -> None:
        """Mixed line endings in such a file survive, since nothing is generated into it."""
        self.make_skill("foo")
        path = self.skills_src / "foo" / "SKILL.md"
        path.write_bytes(b"one\r\ntwo\nthree\r\n")
        build_dist.regenerate()
        self.assertEqual(path.read_bytes(), b"one\r\ntwo\nthree\r\n")

    def test_a_hand_edited_region_reports_stale(self) -> None:
        """Acceptance: build_dist.py --check fails when an include region is edited by hand."""
        self.make_skill("foo", self.region("RULES.md > Alpha"))
        build_dist.regenerate()
        path = self.skills_src / "foo" / "SKILL.md"
        path.write_text(
            self.skill_text().replace("Alpha rule.", "Alpha rule, reworded."), encoding="utf-8"
        )
        self.assertEqual(build_dist.include_drift(), [".agents/skills/foo/SKILL.md"])
        self.assertTrue(build_dist.is_stale())

    def test_an_edited_source_section_reports_stale(self) -> None:
        self.make_skill("foo", self.region("RULES.md > Alpha"))
        build_dist.regenerate()
        (self.tmp / "RULES.md").write_text(
            self.HOME.replace("Alpha rule.", "Alpha rule, v2."), encoding="utf-8"
        )
        self.assertTrue(build_dist.is_stale())
        build_dist.regenerate()
        self.assertIn("Alpha rule, v2.", self.skill_text())
        self.assertFalse(build_dist.is_stale())

    def test_a_renamed_source_heading_is_a_failure_not_a_stale_result(self) -> None:
        """Acceptance: --check fails when the source moves, and regenerating cannot repair a key."""
        self.make_skill("foo", self.region("RULES.md > Alpha"))
        build_dist.regenerate()
        (self.tmp / "RULES.md").write_text(
            self.HOME.replace("## Alpha", "## Alpha Renamed"), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "no heading 'Alpha'"):
            build_dist.is_stale()
        with self.assertRaisesRegex(ValueError, "no heading 'Alpha'"):
            build_dist.regenerate()

    def test_check_reports_a_broken_key_as_2_and_a_stale_region_as_1(self) -> None:
        self.make_skill("foo", self.region("RULES.md > Alpha"))
        build_dist.regenerate()
        path = self.skills_src / "foo" / "SKILL.md"
        path.write_text(self.skill_text().replace("- Two", "- Two, edited"), encoding="utf-8")
        argv = sys.argv
        try:
            sys.argv = ["build_dist.py", "--check"]
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(build_dist.main(), 1)
            self.assertIn(".agents/skills/foo/SKILL.md", err.getvalue())
            (self.tmp / "RULES.md").write_text("## Other\n\nx\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(build_dist.main(), 2)
        finally:
            sys.argv = argv

    def test_a_heading_matches_case_folded_and_at_any_level_from_two(self) -> None:
        (self.tmp / "RULES.md").write_text(
            "## Top\n\nTop text.\n\n### Inner Rule\n\nInner text.\n\n#### Deeper\n\nDeeper text.\n\n### Next\n\nNext text.\n",
            encoding="utf-8",
        )
        self.make_skill("foo", self.region("RULES.md > inner rule"))
        build_dist.regenerate()
        self.assertIn(
            "Inner text.\n\n#### Deeper\n\nDeeper text.\n\n<!-- /include -->", self.skill_text()
        )
        self.assertNotIn("Next text.", self.skill_text())

    def test_two_matching_headings_are_refused(self) -> None:
        (self.tmp / "RULES.md").write_text(
            "## Same\n\na\n\n## Other\n\nb\n\n## same\n\nc\n", encoding="utf-8"
        )
        self.make_skill("foo", self.region("RULES.md > Same"))
        with self.assertRaisesRegex(ValueError, "2 headings match"):
            build_dist.regenerate()

    def test_a_heading_and_a_marker_inside_a_code_fence_are_content(self) -> None:
        (self.tmp / "RULES.md").write_text(
            "## Alpha\n\nReal.\n\n```text\n## Beta\n<!-- include: RULES.md > Beta -->\n```\n\nStill alpha.\n\n## Beta\n\nBeta.\n",
            encoding="utf-8",
        )
        self.make_skill(
            "foo",
            "```markdown\n<!-- include: RULES.md > Nowhere -->\n```\n\n"
            + self.region("RULES.md > Alpha"),
        )
        build_dist.regenerate()
        text = self.skill_text()
        self.assertIn("<!-- include: RULES.md > Nowhere -->\n```", text)
        self.assertIn(
            "```text\n## Beta\n<!-- include: RULES.md > Beta -->\n```\n\nStill alpha.\n\n<!-- /include -->",
            text,
        )
        self.assertFalse(build_dist.is_stale())

    def test_an_include_of_a_file_with_regions_reads_its_filled_text(self) -> None:
        """A region filled from a sibling skill carries what that skill renders, without its markers."""
        self.make_skill(
            "bar", "## Shared\n\n" + self.region("RULES.md > Alpha") + "\n## Own\n\nOwn.\n"
        )
        self.make_skill("foo", self.region(".agents/skills/bar/SKILL.md > Shared"))
        build_dist.regenerate()
        self.assertEqual(
            self.skill_text(),
            "<!-- include: .agents/skills/bar/SKILL.md > Shared -->\n\nAlpha rule.\n\n- One\n- Two\n\n<!-- /include -->\n",
        )

    def test_an_include_cycle_is_refused(self) -> None:
        self.make_skill("bar", "## B\n\n" + self.region(".agents/skills/foo/SKILL.md > A"))
        self.make_skill("foo", "## A\n\n" + self.region(".agents/skills/bar/SKILL.md > B"))
        with self.assertRaisesRegex(ValueError, "include cycle"):
            build_dist.regenerate()

    def test_a_malformed_region_is_refused(self) -> None:
        cases = {
            "no end": "<!-- include: RULES.md > Alpha -->\n",
            "no start": "<!-- /include -->\n",
            "nested": "<!-- include: RULES.md > Alpha -->\n<!-- include: RULES.md > Beta -->\n<!-- /include -->\n",
            "no delimiter": self.region("RULES.md"),
        }
        for label, body in cases.items():
            with self.subTest(label):
                self.make_skill("foo", body)
                with self.assertRaises(ValueError):
                    build_dist.regenerate()

    def test_a_source_outside_the_root_or_under_a_generated_tree_is_refused(self) -> None:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())) / "outside.md"
        outside.write_text("## Alpha\n\nx\n", encoding="utf-8")
        (self.tmp / "link.md").symlink_to(outside)
        self.github_skills.mkdir(parents=True, exist_ok=True)
        (self.github_skills / "gen.md").write_text("## Alpha\n\nx\n", encoding="utf-8")
        for rel in (
            "../outside.md",
            str(outside),
            "link.md",
            ".github/skills/gen.md",
            "missing.md",
        ):
            with self.subTest(rel):
                self.make_skill("foo", self.region(f"{rel} > Alpha"))
                with self.assertRaises(ValueError):
                    build_dist.regenerate()

    def test_a_crlf_skill_keeps_its_endings_when_filled(self) -> None:
        self.make_skill("foo")
        path = self.skills_src / "foo" / "SKILL.md"
        path.write_bytes(b"# Foo\r\n\r\n<!-- include: RULES.md > Beta -->\r\n<!-- /include -->\r\n")
        build_dist.regenerate()
        self.assertEqual(
            path.read_bytes(),
            b"# Foo\r\n\r\n<!-- include: RULES.md > Beta -->\r\n\r\nBeta rule.\r\n\r\n<!-- /include -->\r\n",
        )
        self.assertFalse(build_dist.is_stale())

    def test_a_region_in_a_reference_file_is_filled_too(self) -> None:
        self.make_skill("foo")
        ref = self.skills_src / "foo" / "references" / "notes.md"
        ref.parent.mkdir()
        ref.write_text(self.region("RULES.md > Beta"), encoding="utf-8")
        build_dist.regenerate()
        self.assertIn("Beta rule.", ref.read_text(encoding="utf-8"))
        self.assertIn(
            "Beta rule.",
            (self.github_skills / "foo" / "references" / "notes.md").read_text(encoding="utf-8"),
        )

    def test_mixed_line_endings_in_a_file_with_a_region_are_refused(self) -> None:
        """Rendering would rewrite the rest of the file to one ending, which is the silent flattening the rule forbids."""
        self.make_skill("foo")
        path = self.skills_src / "foo" / "SKILL.md"
        path.write_bytes(b"one\r\n<!-- include: RULES.md > Beta -->\n<!-- /include -->\r\n")
        with self.assertRaisesRegex(ValueError, "mixes line endings"):
            build_dist.regenerate()
        self.assertEqual(
            path.read_bytes(), b"one\r\n<!-- include: RULES.md > Beta -->\n<!-- /include -->\r\n"
        )

    def test_an_empty_heading_body_is_refused(self) -> None:
        (self.tmp / "RULES.md").write_text("## Empty\n\n## Next\n\nx\n", encoding="utf-8")
        self.make_skill("foo", self.region("RULES.md > Empty"))
        with self.assertRaisesRegex(ValueError, "body is empty"):
            build_dist.regenerate()

    def test_an_indented_marker_is_content(self) -> None:
        """Four spaces open an indented code block in CommonMark, so a marker there is a sample, not a region."""
        body = "    <!-- include: RULES.md > Nowhere -->\n    <!-- /include -->\n"
        self.make_skill("foo", body)
        build_dist.regenerate()
        self.assertEqual(self.skill_text(), body)
        self.assertFalse(build_dist.is_stale())

    def test_a_symlinked_skill_directory_is_refused_before_any_fill(self) -> None:
        """The fill writes through whatever the walk found, so the symlink check has to run before it."""
        target = self.tmp / "elsewhere"
        target.mkdir()
        original = self.region("RULES.md > Beta")
        (target / "SKILL.md").write_text(original, encoding="utf-8")
        self.skills_src.mkdir(parents=True, exist_ok=True)
        (self.skills_src / "foo").symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            build_dist.regenerate()
        self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), original)

    def test_a_directory_symlink_on_the_way_is_refused(self) -> None:
        """A symlink inside the root can still alias a generated tree or the including file itself."""
        self.github_skills.mkdir(parents=True, exist_ok=True)
        (self.github_skills / "gen.md").write_text("## Alpha\n\nx\n", encoding="utf-8")
        (self.tmp / "docs").mkdir()
        (self.tmp / "docs" / "link").symlink_to(self.github_skills, target_is_directory=True)
        (self.tmp / "docs" / "alias").symlink_to(self.skills_src / "foo", target_is_directory=True)
        for key in ("docs/link/gen.md > Alpha", "docs/alias/SKILL.md > A"):
            with self.subTest(key):
                self.make_skill("foo", "## A\n\n" + self.region(key))
                with self.assertRaisesRegex(ValueError, "through a symlink"):
                    build_dist.regenerate()

    def test_a_key_spelled_unlike_the_tree_is_refused(self) -> None:
        """A case-insensitive host would resolve it and Linux CI would not, so neither may."""
        self.make_skill("foo", self.region("rules.md > Alpha"))
        with self.assertRaises(ValueError):
            build_dist.regenerate()

    def test_a_missing_source_is_reported_as_missing_and_a_misspelled_one_as_misspelled(
        self,
    ) -> None:
        self.make_skill("foo", self.region("nowhere.md > Alpha"))
        with self.assertRaisesRegex(ValueError, "not a file under the repository root"):
            build_dist.regenerate()
        with self.assertRaisesRegex(ValueError, "spelled as the tree spells it"):
            build_dist._exact_case("rules.md", ("rules.md",))

    def test_a_lone_cr_counts_as_a_third_ending(self) -> None:
        self.make_skill("foo")
        path = self.skills_src / "foo" / "SKILL.md"
        path.write_bytes(
            b"one\r\n\rtwo\r\n<!-- include: RULES.md > Beta -->\r\n<!-- /include -->\r\n"
        )
        with self.assertRaisesRegex(ValueError, "mixes line endings"):
            build_dist.regenerate()
        path.write_bytes(b"<!-- include: RULES.md > Beta -->\r<!-- /include -->\r")
        build_dist.regenerate()
        self.assertEqual(
            path.read_bytes(),
            b"<!-- include: RULES.md > Beta -->\r\rBeta rule.\r\r<!-- /include -->\r",
        )

    def test_a_source_with_text_around_its_own_region_renders_single_blank_lines(self) -> None:
        self.make_skill(
            "bar", "## Shared\n\nIntro.\n\n" + self.region("RULES.md > Beta") + "\nOutro.\n"
        )
        self.make_skill("foo", self.region(".agents/skills/bar/SKILL.md > Shared"))
        build_dist.regenerate()
        self.assertEqual(
            self.skill_text(),
            "<!-- include: .agents/skills/bar/SKILL.md > Shared -->\n\nIntro.\n\nBeta rule.\n\nOutro.\n\n<!-- /include -->\n",
        )

    def test_a_region_in_a_file_the_walk_does_not_visit_is_refused(self) -> None:
        """Such a region would read filled to an includer and stay empty on disk."""
        (self.tmp / "RULES.md").write_text(
            "## Alpha\n\n" + self.region("RULES.md > Beta") + "\n## Beta\n\nb\n", encoding="utf-8"
        )
        self.make_skill("foo", self.region("RULES.md > Alpha"))
        with self.assertRaisesRegex(ValueError, "does not walk is never filled"):
            build_dist.regenerate()
        readme = self.skills_src / "README.md"
        readme.write_text("## Alpha\n\n" + self.region("RULES.md > Beta") + "\n", encoding="utf-8")
        self.make_skill("foo", self.region(".agents/skills/README.md > Alpha"))
        with self.assertRaisesRegex(ValueError, "does not walk is never filled"):
            build_dist.regenerate()

    def test_a_doubled_blank_line_inside_a_fence_is_kept(self) -> None:
        """Only the blank a dropped marker leaves is collapsed, since a fenced sample's blanks are its text."""
        (self.tmp / "RULES.md").write_text(
            "## Alpha\n\n```python\ndef a():\n    pass\n\n\ndef b():\n    pass\n```\n",
            encoding="utf-8",
        )
        self.make_skill("foo", self.region("RULES.md > Alpha"))
        build_dist.regenerate()
        self.assertIn("    pass\n\n\ndef b():", self.skill_text())

    def test_a_body_leaving_a_fence_open_is_refused(self) -> None:
        (self.tmp / "RULES.md").write_text("## Alpha\n\n```text\nopen\n", encoding="utf-8")
        self.make_skill("foo", self.region("RULES.md > Alpha"))
        with self.assertRaisesRegex(ValueError, "leaves a code fence open"):
            build_dist.regenerate()

    def test_fence_step_adds_the_spec_directory_to_sys_path_once(self) -> None:
        before = len(sys.path)
        for _ in range(3):
            build_dist._fence_step("plain", None, 0)
        self.assertLessEqual(len(sys.path) - before, 1)


if __name__ == "__main__":
    unittest.main()

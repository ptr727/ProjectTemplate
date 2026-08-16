#!/usr/bin/env python3
"""Exercise build_dist.py's regeneration and staleness detection against a crafted skills tree.

Run as `python3 scripts/tests/test_build_dist.py`, or under `python3 -m unittest discover -s scripts/tests`.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_dist


class RegenerateCase(unittest.TestCase):
    """Redirects every module path onto a temp tree so a case never touches this repo's own."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.skills_src = self.tmp / ".agents" / "skills"
        self.dist_plugin = self.tmp / ".claude-plugin" / "fleet-skills"
        self.addCleanup(
            self._restore,
            build_dist.SKILLS_SRC,
            build_dist.DIST_PLUGIN,
            build_dist.PLUGIN_MANIFEST,
            build_dist.DIGEST_STAMP,
        )
        build_dist.SKILLS_SRC = self.skills_src
        build_dist.DIST_PLUGIN = self.dist_plugin
        build_dist.PLUGIN_MANIFEST = self.dist_plugin / ".claude-plugin" / "plugin.json"
        build_dist.DIGEST_STAMP = self.dist_plugin / ".source-digest"

    def _restore(self, src, dist, manifest, stamp) -> None:
        build_dist.SKILLS_SRC = src
        build_dist.DIST_PLUGIN = dist
        build_dist.PLUGIN_MANIFEST = manifest
        build_dist.DIGEST_STAMP = stamp

    def make_skill(self, name: str, body: str = "content") -> None:
        d = self.skills_src / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")

    def test_a_symlink_in_a_skill_directory_is_rejected_by_regenerate(self) -> None:
        """shutil.copytree() follows a symlink by default, which would silently pull content
        from outside .agents/skills/ into the generated, published plugin."""
        self.make_skill("foo")
        (self.skills_src / "foo" / "escape").symlink_to(self.tmp)
        with self.assertRaises(ValueError):
            build_dist.regenerate()

    def test_a_symlink_in_a_skill_directory_is_rejected_by_source_digest(self) -> None:
        self.make_skill("foo")
        (self.skills_src / "foo" / "escape").symlink_to(self.tmp)
        with self.assertRaises(ValueError):
            build_dist.source_digest(["foo"])

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
            build_dist.source_digest(["foo"])

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
        content = build_dist.DIGEST_STAMP.read_bytes()
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
        self.assertFalse(build_dist.is_stale())

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
        """.source-digest, the manifest, and SKILL.md's mere presence can all stay untouched
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

    def test_digest_uses_forward_slashes_regardless_of_platform(self) -> None:
        """A digest built from str(Path) would use backslashes on Windows and disagree with a
        Linux machine over identical bytes. Reproduce the expected posix-joined hash by hand and
        confirm it matches, which would fail if source_digest regressed to str(Path)."""
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
        self.assertEqual(build_dist.source_digest(["foo"]), expected.hexdigest()[:16])

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


if __name__ == "__main__":
    unittest.main()

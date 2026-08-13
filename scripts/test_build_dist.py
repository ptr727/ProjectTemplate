#!/usr/bin/env python3
"""Exercise build_dist.py's regeneration and staleness detection against a crafted skills tree.

Run as `python3 scripts/test_build_dist.py`, or under `python3 -m unittest discover -s scripts`.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import build_dist


class RegenerateCase(unittest.TestCase):
    """Redirects every module path onto a temp tree so a case never touches this repo's own."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.skills_src = self.tmp / ".agents" / "skills"
        self.dist_plugin = self.tmp / "dist" / "claude" / "fleet-skills"
        self.addCleanup(self._restore, build_dist.SKILLS_SRC, build_dist.DIST_PLUGIN,
                         build_dist.PLUGIN_MANIFEST, build_dist.DIGEST_STAMP)
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

    def test_regenerate_copies_skill_content_and_lists_it_in_the_manifest(self) -> None:
        self.make_skill("foo")
        self.make_skill("bar")
        names = build_dist.regenerate()
        self.assertEqual(names, ["bar", "foo"])
        manifest = json.loads(build_dist.PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["skills"], ["./skills/bar", "./skills/foo"])
        self.assertEqual((self.dist_plugin / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8"), "content")
        self.assertFalse(build_dist.is_stale())

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


if __name__ == "__main__":
    unittest.main()

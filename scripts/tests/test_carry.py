#!/usr/bin/env python3
"""Exercise carry.py's manifest inventory, safety checks, and apply behavior."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent / "spec"))
import audit
import carry


class CarryInventoryTests(unittest.TestCase):
    def test_compare_reports_missing_modified_extra_and_directories(self) -> None:
        source = carry.Inventory(
            {"same.txt": b"same", "changed.txt": b"new", "missing.txt": b"missing"},
            frozenset({"empty", "nested"}),
            "source",
        )
        target = carry.Inventory(
            {"same.txt": b"same", "changed.txt": b"old", "extra.txt": b"extra"},
            frozenset({"nested", "extra-dir"}),
            "target",
        )

        result = carry.compare(source, target)

        self.assertEqual(result["missing"], ["missing.txt"])
        self.assertEqual(result["modified"], ["changed.txt"])
        self.assertEqual(result["extra"], ["extra.txt"])
        self.assertEqual(result["missingDirectories"], ["empty"])
        self.assertEqual(result["extraDirectories"], ["extra-dir"])

    def test_empty_source_requires_target_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "source"
            source.mkdir()

            result = carry.compare(carry.inventory(source, ["**/*"]), None)

        self.assertTrue(result["missingRoot"])
        self.assertEqual(result["missing"], [])

    def test_inventory_detects_same_size_modification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            left = root / "left"
            right = root / "right"
            left.mkdir()
            right.mkdir()
            (left / "value.txt").write_bytes(b"left")
            (right / "value.txt").write_bytes(b"rite")

            result = carry.compare(
                carry.inventory(left, ["**/*"]), carry.inventory(right, ["**/*"])
            )

        self.assertEqual(result["modified"], ["value.txt"])

    def test_inventory_digest_includes_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            left = root / "left"
            right = root / "right"
            (left / "left-empty").mkdir(parents=True)
            (right / "right-empty").mkdir(parents=True)

            left_inventory = carry.inventory(left, ["**/*"])
            right_inventory = carry.inventory(right, ["**/*"])

        self.assertNotEqual(left_inventory.digest, right_inventory.digest)

    def test_inventory_rejects_source_and_target_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            tree = root / "tree"
            tree.mkdir()
            (tree / "real.txt").write_text("real", encoding="utf-8")
            (tree / "link.txt").symlink_to("real.txt")

            with self.assertRaisesRegex(carry.CarryError, "symlink is not allowed"):
                carry.inventory(tree, ["**/*"])

    def test_inventory_read_failure_is_not_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = pathlib.Path(temp) / "missing"
            with self.assertRaisesRegex(carry.CarryError, "does not exist"):
                carry.inventory(missing, ["**/*"])

    def test_apply_is_idempotent_and_preserves_unrelated_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source_root = root / "source"
            target_root = root / "repo" / "owned"
            unrelated = root / "repo" / "keep.txt"
            source_root.mkdir()
            (source_root / "empty").mkdir()
            target_root.mkdir(parents=True)
            (source_root / "current.txt").write_text("current", encoding="utf-8")
            (target_root / "retired.txt").write_text("retired", encoding="utf-8")
            unrelated.write_text("keep", encoding="utf-8")
            source = carry.inventory(source_root, ["**/*"])
            first = carry.compare(source, carry.inventory(target_root, ["**/*"]))

            changes = carry.apply_tree(source, target_root, root / "repo", first)
            final = carry.compare(source, carry.inventory(target_root, ["**/*"]))
            second_changes = carry.apply_tree(source, target_root, root / "repo", final)
            unrelated_content = unrelated.read_text(encoding="utf-8")
            empty_directory_exists = (target_root / "empty").is_dir()

        self.assertEqual(
            changes,
            ["create owned/empty", "write owned/current.txt", "remove owned/retired.txt"],
        )
        self.assertEqual(second_changes, [])
        self.assertEqual(unrelated_content, "keep")
        self.assertTrue(empty_directory_exists)

    def test_apply_reports_empty_target_root_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            repository = root / "repo"
            source_root = root / "source"
            repository.mkdir()
            source_root.mkdir()
            source = carry.inventory(source_root, ["**/*"])
            target_root = repository / "owned"

            changes = carry.apply_tree(source, target_root, repository, carry.compare(source, None))

        self.assertEqual(changes, ["create owned"])

    def test_pruned_target_with_narrow_include_keeps_structural_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source_root = root / "source"
            target_root = root / "target"
            (source_root / "nested").mkdir(parents=True)
            (target_root / "nested").mkdir(parents=True)
            (source_root / "nested/value.txt").write_text("same", encoding="utf-8")
            (target_root / "nested/value.txt").write_text("same", encoding="utf-8")

            result = carry.compare(
                carry.inventory(source_root, ["*.txt"]),
                carry.inventory(target_root, ["**/*"]),
            )

        self.assertEqual(result["extraDirectories"], [])
        self.assertEqual(result["modified"], [])

    def test_apply_prunes_empty_extra_directory_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source_root = root / "source"
            repository = root / "repo"
            target_root = repository / "owned"
            source_root.mkdir()
            (target_root / "extra/empty").mkdir(parents=True)
            source = carry.inventory(source_root, ["**/*"])
            result = carry.compare(source, carry.inventory(target_root, ["**/*"]))

            changes = carry.apply_tree(source, target_root, repository, result)

        self.assertEqual(changes, ["remove owned/extra/empty", "remove owned/extra"])

    def test_apply_prunes_directories_emptied_by_extra_file_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source_root = root / "source"
            repository = root / "repo"
            target_root = repository / "owned"
            source_root.mkdir()
            (target_root / "extra").mkdir(parents=True)
            (target_root / "extra/file.txt").write_text("extra", encoding="utf-8")
            source = carry.inventory(source_root, ["**/*"])
            result = carry.compare(source, carry.inventory(target_root, ["**/*"]))

            changes = carry.apply_tree(source, target_root, repository, result)
            final = carry.compare(source, carry.inventory(target_root, ["**/*"]))

        self.assertEqual(changes, ["remove owned/extra/file.txt", "remove owned/extra"])
        self.assertEqual(final["extra"], [])
        self.assertEqual(final["extraDirectories"], [])


class CarryManifestTests(unittest.TestCase):
    def test_selector_excludes_inapplicable_declaration(self) -> None:
        self.assertFalse(carry.applicable(["python"], {"csharp", "release"}))

    def test_rejects_overlapping_targets(self) -> None:
        declarations = [
            {
                "source": "source-a",
                "target": ".github",
                "fidelity": "verbatim-tree",
                "appliesTo": "*",
                "include": ["**/*"],
                "prune": False,
            },
            {
                "source": "source-b",
                "target": ".github/skills",
                "fidelity": "verbatim-tree",
                "appliesTo": "*",
                "include": ["**/*"],
                "prune": False,
            },
        ]

        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(carry.CarryError, "overlapping tree declarations"),
        ):
            carry.validate_declarations(declarations, pathlib.Path(temp))

    def test_rejects_malformed_tree_declarations(self) -> None:
        valid = {
            "source": "source",
            "target": "target",
            "fidelity": "verbatim-tree",
            "appliesTo": "*",
            "include": ["**/*"],
            "prune": False,
        }
        malformed = [
            None,
            {key: value for key, value in valid.items() if key != "source"},
            {**valid, "unknown": True},
            {**valid, "include": []},
            {**valid, "prune": "false"},
        ]
        with tempfile.TemporaryDirectory() as temp:
            for declaration in malformed:
                with self.subTest(declaration=declaration), self.assertRaises(carry.CarryError):
                    carry.validate_declarations([declaration], pathlib.Path(temp))

    def test_rejects_target_outside_repository(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(carry.CarryError, "repository-relative"),
        ):
            carry.relative_root(pathlib.Path(temp), "../outside")

    def test_rejects_normalized_parent_segment(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(carry.CarryError, "repository-relative"),
        ):
            carry.relative_root(pathlib.Path(temp), "inside/../target")

    def test_rejects_repository_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(carry.CarryError, "below the repository root"),
        ):
            carry.relative_root(pathlib.Path(temp), ".")

    def test_rejects_symlinked_declared_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            real = root / "real"
            real.mkdir()
            (root / "linked").symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(carry.CarryError, "symlink is not allowed"):
                carry.relative_root(root, "linked/tree")

    def test_target_identity_and_unrelated_dirty_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            remote = root / "remote.git"
            clone = root / "clone"
            standalone = root / "standalone"
            worktree = root / "worktree"
            subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
            subprocess.run(["git", "clone", remote, clone], check=True, capture_output=True)
            for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
                subprocess.run(["git", "-C", clone, "config", key, value], check=True)
            (clone / "seed.txt").write_text("seed", encoding="utf-8")
            subprocess.run(["git", "-C", clone, "add", "seed.txt"], check=True)
            subprocess.run(
                ["git", "-C", clone, "commit", "-m", "Seed"], check=True, capture_output=True
            )
            subprocess.run(["git", "-C", clone, "branch", "-M", "develop"], check=True)
            subprocess.run(
                ["git", "-C", clone, "push", "-u", "origin", "develop"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", clone, "worktree", "add", "-b", "feature/test", worktree, "develop"],
                check=True,
                capture_output=True,
            )
            owned = worktree / "owned"

            subprocess.run(
                ["git", "clone", "--branch", "develop", remote, standalone],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", standalone, "switch", "-c", "feature/standalone"],
                check=True,
                capture_output=True,
            )
            carry.verify_target(standalone, {"url": str(remote)}, [standalone / "owned"])

            with self.assertRaisesRegex(carry.CarryError, "origin does not match"):
                carry.verify_target(worktree, {"url": str(root / "other.git")}, [owned])

            (worktree / "unrelated.txt").write_text("dirty", encoding="utf-8")
            with self.assertRaisesRegex(carry.CarryError, "unrelated changes"):
                carry.verify_target(worktree, {"url": str(remote)}, [owned])
            (worktree / "unrelated.txt").unlink()

            owned.mkdir()
            subprocess.run(["git", "-C", worktree, "mv", "seed.txt", "owned/seed.txt"], check=True)
            with self.assertRaisesRegex(carry.CarryError, "unrelated changes"):
                carry.verify_target(worktree, {"url": str(remote)}, [owned])


HUB_DOC = """# Title

Preamble that the hub and the target share.

## Fleet Bootstrap

The hub's current wording.

```text
## Not A Heading
```

## Where the Rules Live

The hub's current table.
"""

TARGET_DOC = """# Title

Preamble that the hub and the target share.

## Fleet Bootstrap

A wording three revisions behind.

```text
## Not A Heading
```

## Local Addition

A rule this repository wrote after a fault the fleet has never seen.

## Where the Rules Live

The hub's current table.
"""


class CarrySectionTests(unittest.TestCase):
    """The verbatim-section re-vendor: what it replaces, what it must not touch, and what it refuses."""

    def span(self, lines: list[str], heading: str) -> tuple[int, int]:
        """The section's line range, failing the test rather than returning None, so a caller can unpack it."""
        found = carry.section_span(lines, heading)
        if found is None:
            self.fail(f"section '{heading}' was not found")
        return found

    def test_split_lines_round_trips_every_ending(self) -> None:
        for text in ("a\nb\n", "a\r\nb\r\n", "a\r\nb\n", "a\nb", "", "\n"):
            with self.subTest(text=text):
                self.assertEqual("".join(carry.split_lines(text)), text)

    def test_section_span_reads_a_fenced_heading_as_content(self) -> None:
        lines = carry.split_lines(HUB_DOC)

        start, end = self.span(lines, "Fleet Bootstrap")

        self.assertEqual(lines[start], "## Fleet Bootstrap\n")
        self.assertIn("## Not A Heading\n", lines[start:end])
        self.assertEqual(lines[end], "## Where the Rules Live\n")

    def test_section_span_folds_case_and_reports_an_absent_section(self) -> None:
        lines = carry.split_lines(HUB_DOC)

        self.assertEqual(
            carry.section_span(lines, "fleet bootstrap"),
            carry.section_span(lines, "Fleet Bootstrap"),
        )
        self.assertIsNone(carry.section_span(lines, "Release Model"))

    def test_replace_sections_leaves_every_other_byte_identical(self) -> None:
        """The pilot's own defect: a rebuild that drops the blank line before the first heading."""
        target = carry.split_lines(TARGET_DOC)
        source = carry.split_lines(HUB_DOC)

        result = "".join(carry.replace_sections(target, source, ["Fleet Bootstrap"], "AGENTS.md"))

        self.assertIn("The hub's current wording.", result)
        self.assertNotIn("A wording three revisions behind.", result)
        self.assertIn("A rule this repository wrote", result)
        self.assertEqual(
            carry.outside_sections(carry.split_lines(result), ["Fleet Bootstrap"], "AGENTS.md"),
            carry.outside_sections(target, ["Fleet Bootstrap"], "AGENTS.md"),
        )
        self.assertEqual(carry.h2_headings(carry.split_lines(result)), carry.h2_headings(target))

    def test_replace_sections_preserves_the_targets_line_endings(self) -> None:
        target = carry.split_lines(TARGET_DOC.replace("\n", "\r\n"))
        source = carry.split_lines(HUB_DOC)

        result = "".join(carry.replace_sections(target, source, ["Fleet Bootstrap"], "AGENTS.md"))

        self.assertNotIn("\n", result.replace("\r\n", ""))
        self.assertIn("The hub's current wording.\r\n", result)

    def test_replace_sections_terminates_a_region_the_hub_ended_unterminated(self) -> None:
        source = carry.split_lines(HUB_DOC.rstrip("\n"))
        target = carry.split_lines(
            "# Title\n\n## Where the Rules Live\n\nStale.\n\n## Local Addition\n\nKept.\n"
        )

        result = "".join(
            carry.replace_sections(target, source, ["Where the Rules Live"], "AGENTS.md")
        )

        self.assertIn("The hub's current table.\n\n## Local Addition\n", result)
        self.assertEqual(carry.h2_headings(carry.split_lines(result)), carry.h2_headings(target))

    def _unit(self, hub_text: str, target_text: str | None, sections: list[str]) -> None:
        """Plan one AGENTS.md unit from two documents written to temporary hub and target trees."""
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            hub, target = root / "hub", root / "target"
            hub.mkdir()
            target.mkdir()
            (hub / "AGENTS.md").write_bytes(hub_text.encode("utf-8"))
            if target_text is not None:
                (target / "AGENTS.md").write_bytes(target_text.encode("utf-8"))
            carry.plan_unit(hub, target, "AGENTS.md", sections)

    def test_plan_unit_refuses_a_file_the_target_does_not_carry(self) -> None:
        with self.assertRaisesRegex(carry.CarryError, "absent from the target, which is a standup"):
            self._unit(HUB_DOC, None, ["Fleet Bootstrap"])

    def test_plan_unit_refuses_a_section_absent_from_the_hub(self) -> None:
        with self.assertRaisesRegex(carry.CarryError, "absent from the hub's own copy"):
            self._unit(HUB_DOC, TARGET_DOC, ["Release Model"])

    def test_plan_unit_refuses_a_section_absent_from_the_target(self) -> None:
        hub = HUB_DOC + "\n## Release Model\n\nHub only.\n"
        with self.assertRaisesRegex(carry.CarryError, "absent from the target"):
            self._unit(hub, TARGET_DOC, ["Release Model"])

    def test_a_section_ending_the_hub_file_keeps_a_blank_line_before_what_follows(self) -> None:
        """The hub's last section carries no blank line before a following heading, because it has
        none. `spec/audit.py` reads that and one blank line as the same content, so the boundary is
        reconciled to what the target document needs rather than copied byte-for-byte."""
        hub = carry.split_lines(HUB_DOC)
        target = carry.split_lines(
            TARGET_DOC + "\n## Repository Local Rule\n\nWritten after a local fault.\n"
        )

        result = "".join(carry.replace_sections(target, hub, ["Where the Rules Live"], "AGENTS.md"))

        self.assertIn("The hub's current table.\n\n## Repository Local Rule\n", result)
        self.assertIn("Written after a local fault.", result)
        self.assertEqual(
            audit.extract_section(result, "Where the Rules Live"),
            audit.extract_section(HUB_DOC, "Where the Rules Live"),
        )

    def test_a_section_ending_the_target_file_keeps_no_trailing_blank_line(self) -> None:
        """The mirror case: nothing follows the section downstream, so the hub's own trailing blank
        line would leave the file ending on one."""
        hub = carry.split_lines(HUB_DOC + "\n## Release Model\n\nHub only.\n")
        target = carry.split_lines("# Title\n\n## Where the Rules Live\n\nStale.\n")

        result = "".join(carry.replace_sections(target, hub, ["Where the Rules Live"], "AGENTS.md"))

        self.assertTrue(result.endswith("The hub's current table.\n"))
        self.assertEqual(
            audit.extract_section(result, "Where the Rules Live"),
            audit.extract_section(
                HUB_DOC + "\n## Release Model\n\nHub only.\n", "Where the Rules Live"
            ),
        )

    def test_comparable_region_matches_the_fidelity_check_across_the_file_boundary(self) -> None:
        """`region_text` and `extract_section` differ by one newline exactly when one copy's section
        ends its file and the other's does not, which is the case this form exists to reconcile."""
        hub = "# T\n\n## X\n\nbody\n"
        padded = "# T\n\n## X\n\nbody\n\n## Y\n\nlocal\n"

        hub_span = self.span(carry.split_lines(hub), "X")
        padded_span = self.span(carry.split_lines(padded), "X")

        self.assertEqual(
            carry.comparable_region(carry.split_lines(hub), hub_span),
            carry.comparable_region(carry.split_lines(padded), padded_span),
        )
        self.assertEqual(
            audit.extract_section(hub, "X"),
            carry.comparable_region(carry.split_lines(hub), hub_span),
        )
        self.assertNotEqual(
            carry.region_text(carry.split_lines(hub), hub_span),
            carry.region_text(carry.split_lines(padded), padded_span),
        )

    def test_plan_sections_refuses_on_a_later_unit_before_anything_is_written(self) -> None:
        """Every refusal is reachable while planning, so a bad second unit stops the run with the
        first one still unwritten."""
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            hub, target = root / "hub", root / "target"
            hub.mkdir()
            target.mkdir()
            for tree, text in ((hub, HUB_DOC), (target, TARGET_DOC)):
                (tree / "AGENTS.md").write_bytes(text.encode("utf-8"))
            (hub / "GOVERNANCE.md").write_bytes(HUB_DOC.encode("utf-8"))
            before = (target / "AGENTS.md").read_bytes()

            with self.assertRaisesRegex(carry.CarryError, "absent from the target"):
                carry.plan_sections(
                    hub,
                    target,
                    [("AGENTS.md", ["Fleet Bootstrap"]), ("GOVERNANCE.md", ["Fleet Bootstrap"])],
                )

            self.assertEqual((target / "AGENTS.md").read_bytes(), before)

    def test_apply_plans_writes_every_stale_unit_and_asserts_each(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            hub, target = root / "hub", root / "target"
            hub.mkdir()
            target.mkdir()
            for name in ("AGENTS.md", "GOVERNANCE.md"):
                (hub / name).write_bytes(HUB_DOC.encode("utf-8"))
                (target / name).write_bytes(TARGET_DOC.encode("utf-8"))
            units = [("AGENTS.md", ["Fleet Bootstrap"]), ("GOVERNANCE.md", ["Fleet Bootstrap"])]

            plans = carry.plan_sections(hub, target, units)
            carry.apply_plans(target, plans)

            for name in ("AGENTS.md", "GOVERNANCE.md"):
                written = (target / name).read_text(encoding="utf-8")
                self.assertIn("The hub's current wording.", written)
                self.assertIn("A rule this repository wrote", written)
            self.assertEqual([plan.stale for plan in plans], [["Fleet Bootstrap"]] * 2)

    def test_section_units_refuses_a_placeholder_declared_on_a_sibling_entry(self) -> None:
        """The two can sit on separate entries for one path, and the file is written once."""
        manifest = {
            "baseline": [
                {"path": "AGENTS.md", "placeholders": ["<owner>"]},
                {
                    "path": "AGENTS.md",
                    "sections": [{"name": "Fleet Bootstrap", "fidelity": "verbatim"}],
                },
            ]
        }

        with self.assertRaisesRegex(carry.CarryError, "placeholders and verbatim sections"):
            carry.section_units(manifest, {"dotnet"})

    def test_plan_unit_refuses_a_bare_carriage_return(self) -> None:
        with self.assertRaisesRegex(carry.CarryError, "bare carriage return"):
            self._unit(HUB_DOC, TARGET_DOC.replace("\n", "\r"), ["Fleet Bootstrap"])

    def test_plan_unit_reports_the_stale_sections_and_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            hub, target = root / "hub", root / "target"
            hub.mkdir()
            target.mkdir()
            (hub / "AGENTS.md").write_bytes(HUB_DOC.encode("utf-8"))
            (target / "AGENTS.md").write_bytes(TARGET_DOC.encode("utf-8"))

            plan = carry.plan_unit(
                hub, target, "AGENTS.md", ["Fleet Bootstrap", "Where the Rules Live"]
            )

        self.assertEqual(plan.stale, ["Fleet Bootstrap"])
        self.assertEqual(plan.declared, ["Fleet Bootstrap", "Where the Rules Live"])

    def test_replace_sections_refuses_two_names_resolving_to_one_region(self) -> None:
        target = carry.split_lines(TARGET_DOC)
        source = carry.split_lines(HUB_DOC)

        with self.assertRaisesRegex(carry.CarryError, "two regions that overlap"):
            carry.replace_sections(
                target, source, ["Fleet Bootstrap", "fleet bootstrap"], "AGENTS.md"
            )

    def test_verbatim_section_names_folds_case_when_deduplicating(self) -> None:
        item = {
            "sections": [
                {"name": "Fleet Bootstrap", "fidelity": "verbatim"},
                {"name": "fleet bootstrap ", "fidelity": "verbatim"},
            ]
        }

        self.assertEqual(carry.verbatim_section_names(item, {"dotnet"}), ["Fleet Bootstrap"])

    def test_assert_sections_accepts_a_heading_the_re_vendor_re_cased(self) -> None:
        """A re-cased heading is the drift this tool fixes, so the postcondition must not read the
        corrected casing as the section set having changed."""
        target = carry.split_lines(TARGET_DOC.replace("## Fleet Bootstrap", "## fleet bootstrap"))
        source = carry.split_lines(HUB_DOC)
        rewritten = carry.replace_sections(target, source, ["Fleet Bootstrap"], "AGENTS.md")

        with tempfile.TemporaryDirectory() as temp:
            path = self._write(pathlib.Path(temp), "".join(rewritten))
            carry.assert_sections(
                "AGENTS.md", path, source, ["Fleet Bootstrap"], ["Fleet Bootstrap"], target
            )
            self.assertIn("## Fleet Bootstrap\n", path.read_text(encoding="utf-8"))

    def _write(self, root: pathlib.Path, text: str) -> pathlib.Path:
        path = root / "AGENTS.md"
        path.write_bytes(text.encode("utf-8"))
        return path

    def test_assert_sections_passes_on_a_correct_re_vendor(self) -> None:
        target = carry.split_lines(TARGET_DOC)
        source = carry.split_lines(HUB_DOC)
        rewritten = carry.replace_sections(target, source, ["Fleet Bootstrap"], "AGENTS.md")

        with tempfile.TemporaryDirectory() as temp:
            path = self._write(pathlib.Path(temp), "".join(rewritten))
            carry.assert_sections(
                "AGENTS.md", path, source, ["Fleet Bootstrap"], ["Fleet Bootstrap"], target
            )

    def test_assert_sections_catches_a_dropped_blank_line(self) -> None:
        """Mutation test: the replacer's defect from the pilot, which review and the linters both miss."""
        target = carry.split_lines(TARGET_DOC)
        source = carry.split_lines(HUB_DOC)
        start, _ = self.span(target, "Fleet Bootstrap")
        dropped = list(carry.replace_sections(target, source, ["Fleet Bootstrap"], "AGENTS.md"))
        self.assertEqual(dropped[start - 1], "\n")
        del dropped[start - 1]

        with tempfile.TemporaryDirectory() as temp:
            path = self._write(pathlib.Path(temp), "".join(dropped))
            with self.assertRaisesRegex(carry.CarryError, "text outside the re-vendored sections"):
                carry.assert_sections(
                    "AGENTS.md", path, source, ["Fleet Bootstrap"], ["Fleet Bootstrap"], target
                )

    def test_assert_sections_catches_a_deleted_local_section(self) -> None:
        target = carry.split_lines(TARGET_DOC)
        source = carry.split_lines(HUB_DOC)

        with tempfile.TemporaryDirectory() as temp:
            path = self._write(pathlib.Path(temp), HUB_DOC)
            with self.assertRaisesRegex(carry.CarryError, "heading sequence changed"):
                carry.assert_sections(
                    "AGENTS.md", path, source, ["Fleet Bootstrap"], ["Fleet Bootstrap"], target
                )

    def test_assert_sections_catches_a_region_that_did_not_take(self) -> None:
        target = carry.split_lines(TARGET_DOC)
        source = carry.split_lines(HUB_DOC)

        with tempfile.TemporaryDirectory() as temp:
            path = self._write(pathlib.Path(temp), TARGET_DOC)
            with self.assertRaisesRegex(carry.CarryError, "does not match the hub's canonical"):
                carry.assert_sections("AGENTS.md", path, source, [], ["Fleet Bootstrap"], target)

    def test_verbatim_section_names_skips_bare_strings_and_intent(self) -> None:
        item = {
            "sections": [
                "A bare string is intent",
                {"name": "Verbatim Everywhere", "fidelity": "verbatim"},
                {"name": "Intent Section", "fidelity": "intent"},
                {"name": "Verbatim Elsewhere", "fidelity": "verbatim", "appliesTo": ["python"]},
            ]
        }

        self.assertEqual(carry.verbatim_section_names(item, {"dotnet"}), ["Verbatim Everywhere"])

    def test_section_units_refuses_a_placeholder_file(self) -> None:
        manifest = {
            "baseline": [
                {
                    "path": "COPILOT.md",
                    "placeholders": ["<owner>"],
                    "sections": [{"name": "Runbook", "fidelity": "verbatim"}],
                }
            ]
        }

        with self.assertRaisesRegex(carry.CarryError, "placeholders and verbatim sections"):
            carry.section_units(manifest, {"dotnet"})

    def test_section_units_refuses_a_non_markdown_file(self) -> None:
        manifest = {
            "baseline": [
                {
                    "path": ".vscode/tasks.json",
                    "sections": [{"name": "Tasks", "fidelity": "verbatim"}],
                }
            ]
        }

        with self.assertRaisesRegex(carry.CarryError, "cannot be declared on"):
            carry.section_units(manifest, {"dotnet"})

    def test_section_units_merges_one_path_declared_twice(self) -> None:
        manifest = {
            "baseline": [
                {
                    "path": "AGENTS.md",
                    "appliesTo": ["dotnet"],
                    "sections": [{"name": "Fleet Bootstrap", "fidelity": "verbatim"}],
                },
                {
                    "path": "AGENTS.md",
                    "appliesTo": ["dotnet"],
                    "sections": [{"name": "Where the Rules Live", "fidelity": "verbatim"}],
                },
            ]
        }

        self.assertEqual(
            carry.section_units(manifest, {"dotnet"}),
            [("AGENTS.md", ["Fleet Bootstrap", "Where the Rules Live"])],
        )

    def test_guard_refuses_a_region_the_fidelity_comparison_normalizes(self) -> None:
        region = "## Job\n\n```yaml\nuses: actions/checkout@" + "a" * 40 + " # v4\n```\n"

        with self.assertRaisesRegex(carry.CarryError, "normalizes away"):
            carry.guard_governed_drift("AGENTS.md", "Job", region, "hub")

        carry.guard_governed_drift("AGENTS.md", "Fleet Bootstrap", HUB_DOC, "hub")

    def test_run_sections_refuses_the_hub_as_a_target(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(carry.CarryError, "never a re-vendor target"),
        ):
            carry.run_sections("check-sections", carry.HUB_NAME, pathlib.Path(temp), carry.ROOT)

    def test_the_manifest_declares_sections_this_hub_actually_carries(self) -> None:
        """The tool reads the real manifest, so a renamed heading must surface here rather than downstream."""
        manifest = carry.load_json(carry.ROOT / "spec/files.json")

        units = carry.section_units(manifest, {"dotnet", "release", "two-phase"})

        self.assertTrue(units)
        for path, names in units:
            lines = carry.split_lines(carry.decode_text(carry.ROOT / path, path))
            for name in names:
                with self.subTest(path=path, section=name):
                    self.assertIsNotNone(carry.section_span(lines, name))


if __name__ == "__main__":
    unittest.main()

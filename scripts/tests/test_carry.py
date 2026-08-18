#!/usr/bin/env python3
"""Exercise carry.py's manifest inventory, safety checks, and apply behavior."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
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


if __name__ == "__main__":
    unittest.main()

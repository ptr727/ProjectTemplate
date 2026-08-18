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

        self.assertEqual(changes, ["write owned/current.txt", "remove owned/retired.txt"])
        self.assertEqual(second_changes, [])
        self.assertEqual(unrelated_content, "keep")


class CarryManifestTests(unittest.TestCase):
    def test_selector_excludes_inapplicable_declaration(self) -> None:
        self.assertFalse(carry.applicable(["python"], {"csharp", "release"}))

    def test_rejects_overlapping_pruned_targets(self) -> None:
        declarations = [
            {
                "source": "source-a",
                "target": ".github",
                "fidelity": "verbatim-tree",
                "prune": True,
            },
            {
                "source": "source-b",
                "target": ".github/skills",
                "fidelity": "verbatim-tree",
                "prune": False,
            },
        ]

        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(carry.CarryError, "overlapping tree declarations"),
        ):
            carry.validate_declarations(declarations, pathlib.Path(temp))

    def test_rejects_target_outside_repository(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(carry.CarryError, "escapes repository root"),
        ):
            carry.relative_root(pathlib.Path(temp), "../outside")

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


if __name__ == "__main__":
    unittest.main()

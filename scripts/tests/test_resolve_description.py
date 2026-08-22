#!/usr/bin/env python3
"""Exercise resolve_description()'s registry-shape and fail-loud guards directly."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "spec"))
import resolve_description


class ResolveDescriptionCase(unittest.TestCase):
    """repo-config/configure.sh's declared-description resolution (spec/resolve_description.py)."""

    def test_a_repo_with_no_declared_description_resolves_to_none(self) -> None:
        registry = {"repos": [{"name": "Fixture"}]}
        self.assertIsNone(resolve_description.resolve_description(registry, "Fixture"))

    def test_a_repo_absent_from_the_registry_resolves_to_none(self) -> None:
        registry = {"repos": [{"name": "Other"}]}
        self.assertIsNone(resolve_description.resolve_description(registry, "Fixture"))

    def test_a_valid_declared_description_is_returned(self) -> None:
        registry = {"repos": [{"name": "Fixture", "description": "A short tagline."}]}
        self.assertEqual(
            resolve_description.resolve_description(registry, "Fixture"), "A short tagline."
        )

    def test_a_duplicate_name_raises_rather_than_picking_one(self) -> None:
        registry = {
            "repos": [
                {"name": "Fixture", "description": "First."},
                {"name": "Fixture", "description": "Second."},
            ]
        }
        with self.assertRaises(resolve_description.ResolveError):
            resolve_description.resolve_description(registry, "Fixture")

    def test_an_invalid_declared_description_raises(self) -> None:
        registry = {"repos": [{"name": "Fixture", "description": None}]}
        with self.assertRaises(resolve_description.ResolveError):
            resolve_description.resolve_description(registry, "Fixture")

    def test_a_registry_with_no_repos_array_raises_rather_than_reading_as_no_match(self) -> None:
        with self.assertRaises(resolve_description.ResolveError):
            resolve_description.resolve_description({}, "Fixture")

    def test_a_repos_value_that_is_not_a_list_raises(self) -> None:
        with self.assertRaises(resolve_description.ResolveError):
            resolve_description.resolve_description({"repos": "not-a-list"}, "Fixture")

    def test_a_padded_name_that_would_otherwise_match_raises_rather_than_reading_as_absent(
        self,
    ) -> None:
        registry = {"repos": [{"name": " Fixture ", "description": "A short tagline."}]}
        with self.assertRaises(resolve_description.ResolveError):
            resolve_description.resolve_description(registry, "Fixture")


if __name__ == "__main__":
    unittest.main()

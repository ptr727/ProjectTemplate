#!/usr/bin/env python3
"""Assert every tool in this repository decodes a child process as UTF-8 rather than as the locale.

Python decodes a `text=True` pipe with the locale encoding when the call names none. On Linux
that is UTF-8 and the default is invisible. On Windows it is the ANSI code page, commonly
cp1252, so output carrying a byte sequence that code page cannot map raises UnicodeDecodeError.
The tools here read `git`, `gh`, and `jq`, all of which emit UTF-8 on every host, so naming the
encoding is what makes a Windows run read what a Linux run reads.

A test rather than a lint rule because no linter in this repository's toolchain checks it.
`ruff` covers the same omission on `open` and `read_text` and reaches no subprocess call.

Run as `python3 scripts/tests/test_tooling_encoding.py`, or under
`python3 -m unittest discover -s scripts/tests`.
"""

from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# The spawning callables whose text mode this rule governs.
SPAWNERS = frozenset({"run", "check_output", "check_call", "Popen"})

# The keywords that put a call into text mode, either of which makes the encoding load bearing.
TEXT_KEYWORDS = ("text", "universal_newlines")

# One deliberate omission, keyed by the test that owns it rather than by a line number that moves.
# That case proves its cp1252 patch reaches the decoder, and naming an encoding would defeat it.
EXEMPT_FUNCTIONS = frozenset({"test_the_locale_patch_reaches_the_decoder"})

# A floor on the call count, so a scan that matched nothing cannot report itself as clean.
MINIMUM_CALLS = 70


def scanned_python_files() -> list[Path]:
    """Every Python file this checkout holds, tracked or newly added and not ignored.

    An untracked file is the whole of what a change adds, so a scan reading only the index
    passes over the one file a new call is most likely to arrive in.
    """
    names: set[str] = set()
    for extra in ([], ["--others", "--exclude-standard"]):
        listed = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z", *extra, "--", "*.py"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        names.update(name for name in listed.split("\0") if name)
    return sorted(REPO / name for name in names)


def spawning_calls(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    """Each call to a subprocess spawner, paired with the name of the function enclosing it."""
    found: list[tuple[ast.Call, str]] = []

    def walk(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = enclosing
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            if isinstance(child, ast.Call) and _spawner_name(child.func) in SPAWNERS:
                found.append((child, name))
            walk(child, name)

    walk(tree, "")
    return found


def _spawner_name(func: ast.expr) -> str | None:
    """The bare callable name, whether the call was qualified by the module or imported directly."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.attr if func.value.id == "subprocess" else None
    return func.id if isinstance(func, ast.Name) else None


def in_text_mode(call: ast.Call) -> bool:
    """Whether this call asked for decoded output, which is what makes an encoding apply at all."""
    for keyword in call.keywords:
        if keyword.arg in TEXT_KEYWORDS:
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value is True:
                return True
    return False


def names_utf8(call: ast.Call) -> bool:
    """Whether this call pins its encoding to the one every tool it spawns actually emits."""
    for keyword in call.keywords:
        if keyword.arg == "encoding":
            value = keyword.value
            return isinstance(value, ast.Constant) and value.value == "utf-8"
    return False


class TestEveryTextModeSpawnNamesUtf8(unittest.TestCase):
    """The invariant this repository's Windows hosts depend on, asserted over the whole tree."""

    def setUp(self) -> None:
        self.offenders: list[str] = []
        self.counted = 0
        for path in scanned_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for call, enclosing in spawning_calls(tree):
                if not in_text_mode(call):
                    continue
                if enclosing in EXEMPT_FUNCTIONS:
                    continue
                self.counted += 1
                if not names_utf8(call):
                    rel = path.relative_to(REPO)
                    self.offenders.append(f"{rel}:{call.lineno}")

    def test_no_text_mode_spawn_leaves_its_encoding_to_the_locale(self) -> None:
        """A call listed here reads git differently on Windows than it does in CI."""
        self.assertEqual(
            [],
            self.offenders,
            'text=True without encoding="utf-8" at: ' + ", ".join(self.offenders),
        )

    def test_the_scan_reached_the_calls_it_claims_to_cover(self) -> None:
        """A scan that matched nothing passes the case above while proving nothing."""
        self.assertGreaterEqual(self.counted, MINIMUM_CALLS)

    def test_the_exemption_still_names_a_case_that_exists(self) -> None:
        """An exemption outliving its test silently widens the hole it was cut for."""
        owner = REPO / "scripts" / "tests" / "test_prose_lint.py"
        source = owner.read_text(encoding="utf-8")
        for name in EXEMPT_FUNCTIONS:
            self.assertIn(f"def {name}(", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)

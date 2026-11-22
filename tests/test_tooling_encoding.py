#!/usr/bin/env python3
"""Assert every tool in this repository decodes a child process as UTF-8 rather than as the locale.

Python decodes a `text=True` pipe with the locale encoding when the call names none. On Linux that
is UTF-8 and the default is invisible. On Windows it is the ANSI code page, commonly cp1252. So
output carrying a byte sequence that code page cannot map raises UnicodeDecodeError. The tools these
calls spawn do not transcode to the console's code page. So a Windows run taking the ANSI default
reads something the tool never wrote. Naming UTF-8 is what makes that run read what a Linux run
reads. What a byte outside UTF-8 is worth is a separate question this rule does not answer. A call
that can meet one answers it with its own `errors`, as `prose_lint.py` and `host_gate.py` do. The
second matters most, since the tool it runs comes from a declaration a downstream repository can
extend.

A Python child owes the matching half. Below Python 3.15 its own streams take the locale codec
too. So a parent decoding as UTF-8 reads a Windows child's ANSI output as bytes UTF-8 rejects.
`PYTHONIOENCODING=utf-8` in the child's environment reaches all three of its standard streams, so
every text-mode call spawning one passes it. The scan sees a child whose command starts with
`sys.executable` or a literal interpreter name. It follows a name bound in the calling function,
class, or module to find one. An interpreter path found at run time is out of its reach.

A test rather than a lint rule because nothing in this repository's toolchain checks it. `ruff` has
no rule reaching a subprocess call at all. Its rule for the same omission on `open` and `read_text`,
`PLW1514`, covers neither. This repository does not select it in any case, because `pyproject.toml`
extends the default set with `I` alone and that rule is preview-gated.

Run as `python3 tests/test_tooling_encoding.py`, or under
`python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import ast
import re
import subprocess
import unittest
from collections.abc import Iterator
from pathlib import Path, PurePath
from typing import NamedTuple

REPO = Path(__file__).resolve().parent.parent

# The spawning callables whose text mode this rule governs.
SPAWNERS = frozenset({"run", "check_output", "check_call", "Popen"})

# The keywords that put a call into text mode alongside `errors`, checked in `in_text_mode`.
TEXT_KEYWORDS = ("text", "universal_newlines")

# Local dependency and cache trees the untracked scan must not read.
# This repository's `.gitignore` covers `.venv/` but not `venv/`, so a bare `python3 -m venv venv` leaves an unignored, untracked tree behind.
# Pip's vendored packages under that tree carry this exact pattern, and reading them would fail this test on offenders nobody here can fix.
UNTRACKED_EXCLUDES = tuple(
    f":(glob,exclude)**/{name}/**"
    for name in ("node_modules", "venv", ".venv", "site-packages", "__pycache__")
)

# One deliberate omission, keyed by (repository-relative path, function name) instead of by a bare name a future function elsewhere could reuse.
# It is keyed that way instead of by a line number that moves.
# That case proves its cp1252 patch reaches the decoder, and naming an encoding would defeat it.
EXEMPT_FUNCTIONS = frozenset({("tests/test_prose_lint.py", "locale_patch_bites")})

# One deliberate Python child given the ANSI code page, keyed the same way, and exempt from the child check alone.
# That case proves the gate's own UTF-8 reconfigure, which a child already writing UTF-8 would hide.
EXEMPT_CHILDREN = frozenset({("tests/test_spec_validate.py", "gate_on_an_ansi_console")})

# The two production gates this scan was written for, and the ones a maintainer runs locally before a push.
# So the scan reaching them is asserted by name rather than only by count.
GATE_FILES = (
    ".github/actions/prose-gate/prose_lint.py",
    ".github/actions/repo-gate/repo_gate.py",
)

# A floor on the call count, so a scan that matched nothing cannot report itself as clean.
MINIMUM_CALLS = 70

# The same floor for the calls whose child is a Python interpreter.
MINIMUM_PYTHON_CHILDREN = 40

# A command's first word naming an interpreter, matched against its final path component.
PYTHON_LAUNCHER = re.compile(r"(?:python[0-9.]*|py)(?:\.exe)?", re.IGNORECASE)

# The child-side variable and the one value that matches a parent decoding as UTF-8.
CHILD_ENCODING = ("PYTHONIOENCODING", "utf-8")


def scanned_python_files() -> list[Path]:
    """Every Python file this checkout holds, tracked or newly added and not ignored.

    An untracked file is the whole of what a change adds. So a scan reading only the index passes
    over the one file a new call is most likely to arrive in. A local virtual environment is
    untracked too, so the untracked pass excludes the trees named in `UNTRACKED_EXCLUDES` the same
    way `docker_lint.py` excludes `node_modules`.
    """
    names: set[str] = set()
    for extra in ([], ["--others", "--exclude-standard"]):
        # The excludes apply to the untracked pass alone.
        # A tracked file under one of those names is still read rather than silently dropped.
        excludes = UNTRACKED_EXCLUDES if extra else ()
        listed = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z", *extra, "--", "*.py", *excludes],
            capture_output=True,
            text=True,
            encoding="utf-8",
            # A name git emits raw under -z need not be UTF-8.
            # A scan claiming to be exhaustive cannot end on the one file whose name it missed.
            errors="surrogateescape",
            check=True,
        ).stdout
        names.update(name for name in listed.split("\0") if name)
    return sorted(REPO / name for name in names)


def _subprocess_bindings(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """This file's own local names for the `subprocess` module and for a spawner imported by name.

    `import subprocess as sp` and `from subprocess import run as spawn` are both lint-clean. So a
    scan matching only the literal `subprocess.run` spelling silently drops such a file from
    coverage with no failing test to say so.
    """
    modules: set[str] = set()
    spawners: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in SPAWNERS:
                    spawners.add(alias.asname or alias.name)
    return frozenset(modules), frozenset(spawners)


def _is_spawner_call(func: ast.expr, modules: frozenset[str], spawners: frozenset[str]) -> bool:
    """Whether this call target resolves, through this file's own imports, to a subprocess spawner."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id in modules and func.attr in SPAWNERS
    if isinstance(func, ast.Name):
        return func.id in spawners
    return False


class Spawn(NamedTuple):
    """One spawning call and the scopes a name it uses can be bound in, innermost first."""

    call: ast.Call
    enclosing: str
    functions: tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]
    cls: ast.ClassDef | None
    module: ast.Module


def spawning_calls(tree: ast.Module) -> list[Spawn]:
    """Each call to a subprocess spawner, with the function enclosing it and its other scopes."""
    modules, spawners = _subprocess_bindings(tree)
    found: list[Spawn] = []

    def walk(
        node: ast.AST,
        functions: tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...],
        cls: ast.ClassDef | None,
    ) -> None:
        for child in ast.iter_child_nodes(node):
            inner_functions, inner_cls = functions, cls
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                inner_functions = (child, *functions)
            elif isinstance(child, ast.ClassDef):
                inner_cls = child
            if isinstance(child, ast.Call) and _is_spawner_call(child.func, modules, spawners):
                enclosing = functions[0].name if functions else ""
                found.append(Spawn(child, enclosing, functions, cls, tree))
            walk(child, inner_functions, inner_cls)

    walk(tree, (), None)
    return found


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Every node in this scope, leaving out the bodies of the functions and classes it defines."""
    for child in ast.iter_child_nodes(scope):
        yield child
        if not isinstance(
            child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
        ):
            yield from _own_nodes(child)


def _target_key(node: ast.expr) -> str | None:
    """A local name as itself and an attribute of `self` as `self.<name>`, or None for any other."""
    if isinstance(node, ast.Name):
        return node.id
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return f"self.{node.attr}"
    return None


def _class_chain(spawn: Spawn) -> list[ast.ClassDef]:
    """The enclosing class and the base classes this module defines for it, at any depth."""
    defined = {n.name: n for n in spawn.module.body if isinstance(n, ast.ClassDef)}
    chain: list[ast.ClassDef] = []
    pending = [spawn.cls] if spawn.cls else []
    while pending:
        cls = pending.pop()
        if cls in chain:
            continue
        chain.append(cls)
        pending.extend(
            defined[b.id] for b in cls.bases if isinstance(b, ast.Name) and b.id in defined
        )
    return chain


class Write(NamedTuple):
    """One value a name is given, and whether it changes the value already there or replaces it."""

    value: ast.expr
    mutates: bool


def _writes(key: str, spawn: Spawn) -> Iterator[Write]:
    """Each expression stored into the name `key`, an item set on it, and each update applied to it.

    The scope searched is the one Python would resolve the name in. For an attribute of `self` that
    is the class and each base class this module defines. A shared `setUp` commonly binds what a
    test method spawns with. For a bare name it is each enclosing function and then the module.
    An item set or an `update` is returned as the dictionary it writes, so the caller reads every
    kind of write the same way.
    """
    if key.startswith("self."):
        nodes: list[ast.AST] = [n for c in _class_chain(spawn) for n in ast.walk(c)]
    else:
        nodes = [node for scope in (*spawn.functions, spawn.module) for node in _own_nodes(scope)]
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _target_key(target) == key:
                    yield Write(node.value, mutates=False)
                elif isinstance(target, ast.Subscript) and _target_key(target.value) == key:
                    yield Write(ast.Dict(keys=[target.slice], values=[node.value]), mutates=True)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if _target_key(node.target) == key:
                yield Write(node.value, mutates=False)
        elif isinstance(node, ast.AugAssign) and _target_key(node.target) == key:
            yield Write(node.value, mutates=True)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and _target_key(node.func.value) == key
        ):
            for arg in node.args:
                yield Write(arg, mutates=True)
            yield Write(
                ast.Call(func=ast.Name("dict"), args=[], keywords=node.keywords), mutates=True
            )


def _is_constant(node: ast.expr | None, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _entries(expr: ast.expr) -> list[ast.expr]:
    """The values this expression itself gives `PYTHONIOENCODING`, as a dictionary entry or a keyword."""
    name = CHILD_ENCODING[0]
    found: list[ast.expr] = []
    for node in ast.walk(expr):
        if isinstance(node, ast.Dict):
            found.extend(
                v for k, v in zip(node.keys, node.values, strict=True) if _is_constant(k, name)
            )
        elif isinstance(node, ast.keyword) and node.arg == name:
            found.append(node.value)
    return found


def _reads(expr: ast.expr, key: str) -> bool:
    return any(_target_key(node) == key for node in ast.walk(expr) if isinstance(node, ast.expr))


def carries_child_encoding(
    expr: ast.expr, spawn: Spawn, seen: frozenset[str] = frozenset()
) -> bool:
    """Whether this environment expression sets `PYTHONIOENCODING` to `utf-8`.

    An entry the expression writes itself decides, a dictionary entry or a `dict(...)` keyword, and
    every one has to be the literal `utf-8`. A value the scan cannot read counts as another codec.
    Without such an entry, a local name or an attribute of `self` the expression reads decides, as
    `_name_carries` reads it. A function's return value is not followed, so an environment a helper
    builds is read as not carrying it.
    """
    entries = _entries(expr)
    if entries:
        return all(_is_constant(v, CHILD_ENCODING[1]) for v in entries)
    for base in _bases(expr):
        key = _target_key(base)
        if key is not None and key not in seen and _name_carries(key, spawn, seen | {key}):
            return True
    return False


def _bases(expr: ast.expr) -> list[ast.expr]:
    """The names an environment expression is built from, rather than merely reads a value out of.

    That is the expression itself, a `**` unpacking, `dict(...)`'s first argument or a `**` keyword
    to it, a `.copy()`, and either side of a `|`. A name read for one item, or filtered through a
    comprehension, is not a base. So it never lends the environment an entry it does not pass.
    """
    if _target_key(expr) is not None:
        return [expr]
    if isinstance(expr, ast.Dict):
        return [
            b for k, v in zip(expr.keys, expr.values, strict=True) if k is None for b in _bases(v)
        ]
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        return _bases(expr.left) + _bases(expr.right)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "dict":
        spread = [k.value for k in expr.keywords if k.arg is None]
        return [b for v in [*expr.args[:1], *spread] for b in _bases(v)]
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "copy"
    ):
        return _bases(expr.func.value)
    return []


def _name_carries(key: str, spawn: Spawn, seen: frozenset[str]) -> bool:
    """Whether every value this name can hold at the spawn sets the variable, read without flow.

    An item set or an `update` changes whatever value the name already holds. So where any of them
    sets the variable, those decide, and each one has to set it to `utf-8`. Otherwise each
    rebinding replaces the value. Every rebinding that does not read the name itself has to carry
    it. So one branch binding a bare environment is not hidden by another binding a full one.
    """
    writes = list(_writes(key, spawn))
    setting = [w.value for w in writes if w.mutates and _entries(w.value)]
    if setting:
        return all(carries_child_encoding(v, spawn, seen) for v in setting)
    fresh = [w.value for w in writes if not w.mutates and not _reads(w.value, key)]
    return bool(fresh) and all(carries_child_encoding(v, spawn, seen) for v in fresh)


def _is_python_head(expr: ast.expr, spawn: Spawn, seen: frozenset[str]) -> bool:
    """Whether a command's first word names a Python interpreter."""
    if (
        isinstance(expr, ast.Attribute)
        and isinstance(expr.value, ast.Name)
        and (expr.value.id, expr.attr) == ("sys", "executable")
    ):
        return True
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return PYTHON_LAUNCHER.fullmatch(PurePath(expr.value).name) is not None
    key = _target_key(expr)
    if key is None or key in seen:
        return False
    return any(_is_python_head(w.value, spawn, seen | {key}) for w in _writes(key, spawn))


def spawns_python(expr: ast.expr, spawn: Spawn, seen: frozenset[str] = frozenset()) -> bool:
    """Whether this command expression starts with a Python interpreter.

    The command is read through a list or tuple display, a leading `*` unpacking, and a `+`
    concatenation. A name bound to any of those is followed as far as the scan can see it.
    """
    if isinstance(expr, ast.List | ast.Tuple):
        if not expr.elts:
            return False
        head = expr.elts[0]
        if isinstance(head, ast.Starred):
            return spawns_python(head.value, spawn, seen)
        return _is_python_head(head, spawn, seen)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return spawns_python(expr.left, spawn, seen)
    key = _target_key(expr)
    if key is None or key in seen:
        return False
    return any(spawns_python(w.value, spawn, seen | {key}) for w in _writes(key, spawn))


def command_of(call: ast.Call) -> ast.expr | None:
    """The command a spawning call runs, passed first or as `args`."""
    if call.args:
        return call.args[0]
    return next((k.value for k in call.keywords if k.arg == "args"), None)


def _command_variants(
    expr: ast.expr, spawn: Spawn, seen: frozenset[str] = frozenset()
) -> list[list[str | None]]:
    """Each word list a command can be, a word the scan cannot read held as None.

    A `*` unpacking, a `+` concatenation, and a bound name are followed. A name bound more than
    once yields one list per rebinding, so a flag one binding carries never clears another. A `+=`
    onto the name extends every one of them.
    """
    if isinstance(expr, ast.Constant):
        return [[expr.value if isinstance(expr.value, str) else None]]
    if isinstance(expr, ast.Starred):
        return _command_variants(expr.value, spawn, seen)
    if isinstance(expr, ast.List | ast.Tuple):
        variants: list[list[str | None]] = [[]]
        for e in expr.elts:
            variants = [v + w for v in variants for w in _command_variants(e, spawn, seen)]
        return variants
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _command_variants(expr.left, spawn, seen)
        return [v + w for v in left for w in _command_variants(expr.right, spawn, seen)]
    key = _target_key(expr)
    if key is None or key in seen:
        return [[None]]
    writes = list(_writes(key, spawn))
    found = [
        variant
        for w in writes
        if not w.mutates
        for variant in _command_variants(w.value, spawn, seen | {key})
    ] or [[None]]
    for w in writes:
        if w.mutates and isinstance(w.value, ast.List | ast.Tuple | ast.Name | ast.BinOp):
            tails = _command_variants(w.value, spawn, seen | {key})
            found = [v + t for v in found for t in tails]
    return found


def _interpreter_options(words: list[str | None]) -> set[str]:
    """The options a command gives its interpreter, read up to its script, `-c`, or `-m`.

    An option taking its value as the next word, `-X` or `-W`, comes back joined to it. So `-X utf8`
    comes back as `-Xutf8`, however it is spelled.
    """
    options: set[str] = set()
    rest = iter(words[1:])
    for word in rest:
        if word is None or not word.startswith("-") or word in ("-c", "-m", "-"):
            break
        if word in ("-X", "-W"):
            word = f"{word}{next(rest, '')}"
        options.add(word)
    return options


def python_child_without_encoding(spawn: Spawn) -> bool | None:
    """None where the child is not Python, else whether it is left to encode by the locale.

    `-X utf8` among the interpreter's own options clears it too, since UTF-8 Mode sets the same
    three streams. It is the one way to reach a child run with `-I` or `-E`, which ignores every
    `PYTHON` variable. So for such a child the environment does not count. Every word list the
    command can be has to be cleared one of those ways.
    """
    command = command_of(spawn.call)
    if command is None or not spawns_python(command, spawn):
        return None
    env = next((k.value for k in spawn.call.keywords if k.arg == "env"), None)
    env_carries = env is not None and carries_child_encoding(env, spawn)
    for words in _command_variants(command, spawn):
        options = _interpreter_options(words)
        if "-Xutf8" in options:
            continue
        if options & {"-I", "-E"} or not env_carries:
            return True
    return False


def in_text_mode(call: ast.Call) -> bool:
    """Whether this call decodes its child's output, which is what makes an encoding load bearing.

    Per the subprocess docs, naming `encoding` or `errors` puts a call into text mode on its own,
    not only `text=True`. Both spellings have to arrive here, since a call this returns False for is
    never judged at all. Each spelling matters for its own reason:

    - `errors="replace"` alone takes the locale and hits the exact bug this file exists to catch.
    - `encoding="cp1252"` is text mode in an encoding git does not emit.
    """
    for keyword in call.keywords:
        if keyword.arg in ("encoding", "errors"):
            return True
        if keyword.arg in TEXT_KEYWORDS:
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value in (False, None, 0):
                continue
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
        self.python_offenders: list[str] = []
        self.counted = 0
        self.python_children = 0
        self.gate_hits: dict[str, int] = {}
        self.exemptions_hit: dict[tuple[str, str], int] = {}
        self.function_name_counts: dict[str, int] = {}
        for path in scanned_python_files():
            rel = path.relative_to(REPO).as_posix()
            try:
                source = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # A tracked file `git ls-files` lists can be removed but not yet staged, or removed by a concurrent process before this reads it.
                # Catching the read instead of checking existence first avoids re-opening that same race.
                continue
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    self.function_name_counts[node.name] = (
                        self.function_name_counts.get(node.name, 0) + 1
                    )
            for spawn in spawning_calls(tree):
                call = spawn.call
                if not in_text_mode(call):
                    continue
                key = (rel, spawn.enclosing)
                if key in EXEMPT_FUNCTIONS:
                    self.exemptions_hit[key] = self.exemptions_hit.get(key, 0) + 1
                    continue
                self.counted += 1
                if rel in GATE_FILES:
                    self.gate_hits[rel] = self.gate_hits.get(rel, 0) + 1
                if not names_utf8(call):
                    self.offenders.append(f"{rel}:{call.lineno}")
                unset = python_child_without_encoding(spawn)
                if unset is not None and key in EXEMPT_CHILDREN:
                    self.exemptions_hit[key] = self.exemptions_hit.get(key, 0) + 1
                elif unset is not None:
                    self.python_children += 1
                    if unset:
                        self.python_offenders.append(f"{rel}:{call.lineno}")

    def test_no_text_mode_spawn_leaves_its_encoding_to_the_locale(self) -> None:
        """A call listed here reads git differently on Windows than it does in CI."""
        self.assertEqual(
            [],
            self.offenders,
            'a text-mode call without encoding="utf-8" at: ' + ", ".join(self.offenders),
        )

    def test_no_python_child_is_left_to_encode_by_the_locale(self) -> None:
        """A call listed here decodes UTF-8 from a child that writes the ANSI code page on Windows."""
        self.assertEqual(
            [],
            self.python_offenders,
            "a text-mode Python child without PYTHONIOENCODING=utf-8 at: "
            + ", ".join(self.python_offenders),
        )

    def test_the_scan_reached_the_calls_it_claims_to_cover(self) -> None:
        """A scan that matched nothing passes the case above while proving nothing."""
        self.assertGreaterEqual(self.counted, MINIMUM_CALLS)
        self.assertGreaterEqual(self.python_children, MINIMUM_PYTHON_CHILDREN)

    def test_the_scan_reached_both_named_gates(self) -> None:
        """A path change under `.github/actions/` could drop a gate out of the scan unnoticed."""
        for rel in GATE_FILES:
            self.assertGreaterEqual(
                self.gate_hits.get(rel, 0), 1, f"no text-mode call counted in {rel}"
            )

    def test_the_exemption_still_names_a_case_that_exists(self) -> None:
        """An exemption outliving its test silently widens the hole it was cut for."""
        for rel, name in EXEMPT_FUNCTIONS | EXEMPT_CHILDREN:
            self.assertEqual(
                1,
                self.function_name_counts.get(name, 0),
                f"{name} is not defined exactly once across the scanned tree",
            )
            self.assertEqual(
                1,
                self.exemptions_hit.get((rel, name), 0),
                f"the exemption for {rel}:{name} covers exactly one call, and did not",
            )


class TestThePythonChildCheckReadsEachShape(unittest.TestCase):
    """Constructed calls, so a reading that stopped matching one shape fails here and not silently."""

    def verdicts(self, source: str) -> list[bool | None]:
        tree = ast.parse(source)
        return [python_child_without_encoding(spawn) for spawn in spawning_calls(tree)]

    def test_a_python_child_with_no_environment_is_flagged(self) -> None:
        source = 'import subprocess, sys\nsubprocess.run([sys.executable, "-c", "x"], text=True, encoding="utf-8")\n'
        self.assertEqual([True], self.verdicts(source))

    def test_an_interpreter_named_by_a_string_is_a_python_child(self) -> None:
        source = 'import subprocess\nsubprocess.run(["/usr/bin/python3.12", "x.py"], encoding="utf-8", env={})\n'
        self.assertEqual([True], self.verdicts(source))

    def test_an_inline_entry_clears_the_call(self) -> None:
        source = (
            "import os, subprocess, sys\n"
            'subprocess.run([sys.executable], encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})\n'
        )
        self.assertEqual([False], self.verdicts(source))

    def test_a_different_encoding_does_not_clear_the_call(self) -> None:
        source = (
            "import subprocess, sys\n"
            'subprocess.run([sys.executable], encoding="utf-8", env={"PYTHONIOENCODING": "cp1252"})\n'
        )
        self.assertEqual([True], self.verdicts(source))

    def test_names_are_followed_through_a_function_and_its_class(self) -> None:
        source = (
            "import os, subprocess, sys\n"
            "class Case:\n"
            "    def setUp(self):\n"
            "        self.env = dict(os.environ)\n"
            '        self.env["PYTHONIOENCODING"] = "utf-8"\n'
            "    def go(self):\n"
            "        argv = [sys.executable, 'x.py']\n"
            "        env = dict(self.env)\n"
            '        subprocess.run([*argv, "--flag"], encoding="utf-8", env=env)\n'
            "    def bare(self):\n"
            "        env = dict(os.environ)\n"
            '        subprocess.run([sys.executable] + ["x"], encoding="utf-8", env=env)\n'
        )
        self.assertEqual([False, True], self.verdicts(source))

    def test_every_rebinding_has_to_carry_it(self) -> None:
        source = (
            "import os, subprocess, sys\n"
            "def go(full):\n"
            "    if full:\n"
            '        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}\n'
            "    else:\n"
            "        env = {}\n"
            '    subprocess.run([sys.executable], encoding="utf-8", env=env)\n'
        )
        self.assertEqual([True], self.verdicts(source))

    def test_an_item_set_to_another_codec_defeats_the_call(self) -> None:
        source = (
            "import os, subprocess, sys\n"
            "def go(codec):\n"
            '    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}\n'
            '    env["PYTHONIOENCODING"] = codec\n'
            '    subprocess.run([sys.executable], encoding="utf-8", env=env)\n'
        )
        self.assertEqual([True], self.verdicts(source))

    def test_an_update_keyword_clears_the_call(self) -> None:
        source = (
            "import os, subprocess, sys\n"
            "def go():\n"
            "    env = dict(os.environ)\n"
            '    env.update(PYTHONIOENCODING="utf-8")\n'
            '    subprocess.run([sys.executable], encoding="utf-8", env=env)\n'
        )
        self.assertEqual([False], self.verdicts(source))

    def test_an_isolated_child_needs_utf8_mode_on_its_command_line(self) -> None:
        source = (
            "import os, subprocess, sys\n"
            'subprocess.run([sys.executable, "-I", "-c", "x"], encoding="utf-8", env={"PYTHONIOENCODING": "utf-8"})\n'
            'subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", "x"], encoding="utf-8")\n'
        )
        self.assertEqual([True, False], self.verdicts(source))

    def test_isolation_flags_are_read_through_a_name_and_an_unpacking(self) -> None:
        source = (
            "import os, subprocess, sys\n"
            'ISOLATED = ("-I",)\n'
            "def go():\n"
            '    cmd = [sys.executable, "-I", "-c", "x"]\n'
            '    subprocess.run(cmd, encoding="utf-8", env={"PYTHONIOENCODING": "utf-8"})\n'
            '    subprocess.run([sys.executable, *ISOLATED, "x.py"], encoding="utf-8", env={"PYTHONIOENCODING": "utf-8"})\n'
            '    fixed = [sys.executable, "-I", "-X", "utf8", "-c", "x"]\n'
            '    subprocess.run(fixed, encoding="utf-8")\n'
        )
        self.assertEqual([True, True, False], self.verdicts(source))

    def test_a_name_read_for_one_item_lends_no_entry(self) -> None:
        source = (
            "import os, subprocess, sys\n"
            "class Case:\n"
            "    def setUp(self):\n"
            '        self.env = {**os.environ, "PYTHONIOENCODING": "utf-8"}\n'
            "    def go(self):\n"
            '        subprocess.run([sys.executable], encoding="utf-8", env={"PATH": self.env["PATH"]})\n'
            "        kept = {k: v for k, v in self.env.items() if k != 'PYTHONIOENCODING'}\n"
            '        subprocess.run([sys.executable], encoding="utf-8", env=kept)\n'
            '        subprocess.run([sys.executable], encoding="utf-8", env=dict(self.env))\n'
        )
        self.assertEqual([True, True, False], self.verdicts(source))

    def test_each_binding_of_a_command_is_judged_on_its_own(self) -> None:
        source = (
            "import subprocess, sys\n"
            "def go():\n"
            '    cmd = [sys.executable, "-I", "-X", "utf8", "-c", "a"]\n'
            '    subprocess.run(cmd, encoding="utf-8")\n'
            '    cmd = [sys.executable, "-I", "-c", "b"]\n'
            '    subprocess.run(cmd, encoding="utf-8")\n'
            "def script_argument():\n"
            '    subprocess.run([sys.executable, "s.py", "-X", "utf8"], encoding="utf-8")\n'
        )
        self.assertEqual([True, True, True], self.verdicts(source))

    def test_a_flag_added_later_or_after_a_valued_option_is_read(self) -> None:
        source = (
            "import subprocess, sys\n"
            "def go():\n"
            "    cmd = [sys.executable]\n"
            '    cmd += ["-I", "-c", "x"]\n'
            '    subprocess.run(cmd, encoding="utf-8", env={"PYTHONIOENCODING": "utf-8"})\n'
            "def warned():\n"
            '    argv = [sys.executable, "-W", "error", "-I", "-c", "x"]\n'
            '    subprocess.run(argv, encoding="utf-8", env={"PYTHONIOENCODING": "utf-8"})\n'
        )
        self.assertEqual([True, True], self.verdicts(source))

    def test_an_attribute_bound_in_a_base_class_is_followed(self) -> None:
        source = (
            "import os, subprocess, sys\n"
            "class Base:\n"
            "    def setUp(self):\n"
            '        self.env = {**os.environ, "PYTHONIOENCODING": "utf-8"}\n'
            "class Case(Base):\n"
            "    def go(self):\n"
            '        subprocess.run([sys.executable], encoding="utf-8", env=self.env)\n'
        )
        self.assertEqual([False], self.verdicts(source))

    def test_a_child_that_is_not_python_is_not_judged(self) -> None:
        source = 'import subprocess\nsubprocess.run(["git", "status"], encoding="utf-8")\n'
        self.assertEqual([None], self.verdicts(source))


if __name__ == "__main__":
    unittest.main(verbosity=2)

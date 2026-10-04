#!/usr/bin/env python3
"""Deterministic pre-push checks for GOVERNANCE.md rules nothing else enforces.

Each check maps to a recurring review-finding category. A count beside one is that
category's share of a 1,047-finding audit of this repo's Copilot reviews:

  sha-pin       Action SHA-pinning gaps                        25 findings  (GOVERNANCE.md rule)
  eol           .editorconfig <-> .gitattributes disagreement  40 findings
  eol-coverage  Git attribute resolution differs from policy   count not recorded
  composite-actions  shellcheck and schema coverage of action.yml  count not recorded

`eol-coverage` asks Git how representative paths resolve. This proves the global text default
reaches Python, shell, Dockerfiles, workflow YAML, and extensionless scripts without maintaining
one pin per path. It also proves the two Windows command-script exceptions stay CRLF.

`sha-pin` reads the shape and then resolves it, because forty hex characters is a format any
fabricated string satisfies, and an agent hand-writing a plausible SHA into a workflow is a
failure this repo has seen rather than a hypothetical one. Resolving also catches the
neighboring case, a pin whose commit was reachable only from a branch since squashed and
deleted, which breaks a downstream gate long after the change that caused it. The
`gh-write-guard` hook cannot cover either, since it watches Bash and an editor tool writing
the same string into a file never reaches it.

A stale-backticked-path check was built and REJECTED: in a template repo, docs
legitimately reference paths that live in downstream repos (`.vscode/tasks.json`,
`Docker/README.md`, `reports/*/audit.md` targets), so it produced 34 false positives
on a clean tree with no way to separate those from real drift. Doc-to-doc drift is
the job of the fresh-context self-review, not a regex.

Read-only. Exit 1 if any check fails. Pair with prose_lint.py, which covers the
house-style prose rules, and with the existing CI linters (markdownlint, cspell,
actionlint, editorconfig-checker, spec/validate.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# GOVERNANCE.md documents exactly one floating-ref exception.
SHA_EXCEPTIONS = {"dotnet/nbgv"}
USES = re.compile(r"^[ \t]*-?[ \t]*uses:\s*(?P<ref>[^\s#]+)", re.MULTILINE)
PIN = re.compile(r"^[0-9a-f]{40}$")
WORKFLOW = re.compile(r"workflows/.*\.ya?ml$")
# What `gh` prints when GitHub answered, as opposed to when nothing was reached at all.
HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
# The two GitHub returns for an object that is not there.
# Everything else is a failure to read, since 401 and 403 are credentials and a rate limit.
# A network error carries no status at all.
# Reading any of those as absence fails a correct pin, which is the direction that costs most.
ABSENT = {"404", "422"}
GH_TIMEOUT = 20
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# How a check says it did less than its name.
# A gate that quietly degrades to a weaker reading prints the same clean line as one that ran.
# The narrowing is therefore printed rather than left to be inferred.
# Never a finding, since nothing is wrong with the tree when the network is what is missing.
NOTES: list[str] = []


def sh(*args: str) -> str:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=False,
    ).stdout


def origin_owner(root: Path) -> str | None:
    """The owner of the repository at `root`, lower-cased, or None where it cannot be read.

    Read from the tree being scanned rather than from this script's own checkout, since the gate
    is hub-hosted and runs against whatever `--root` names.
    """
    url = sh("git", "-C", str(root), "remote", "get-url", "origin").strip()
    m = re.search(r"[:/]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$", url)
    return m.group(1).lower() if m else None


def gh_exists(path: str) -> bool | None:
    """True where GitHub returned the object, False where it answered absent, None where neither.

    None covers an absent `gh`, no credentials, a rate limit, and an offline host, which are one
    thing here: nothing was learned. The caller reports those as skipped rather than as findings,
    so the gate stays usable on a machine with no network instead of failing a correct tree.
    """
    try:
        r = subprocess.run(
            ["gh", "api", path, "--jq", ".sha"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0:
        return True
    m = HTTP_STATUS.search(r.stderr)
    return False if m and m.group(1) in ABSENT else None


def pin_resolves(nwo: str, sha: str, cache: dict[tuple[str, str], bool | None]) -> bool | None:
    """Whether `sha` is a commit in `nwo`, cached, with an unreadable repository read as unknown.

    An absent commit and a repository the credentials cannot see are the same 404 from here, and
    reading the second as the first fails a correct pin whenever the token is narrower than the
    fleet, which a repository-scoped CI token is. So a miss is confirmed against the repository
    itself before it becomes a finding, and that second read runs only on the failing path.
    """
    key = (nwo, sha)
    if key not in cache:
        seen = gh_exists(f"repos/{nwo}/commits/{sha}")
        if seen is False and gh_exists(f"repos/{nwo}") is not True:
            seen = None
        cache[key] = seen
    return cache[key]


def tracked(root: Path, exclude: list[str] | None = None) -> list[str]:
    """Every git-tracked path, narrowed by `exclude` pathspec patterns where the caller gives any.

    Each pattern becomes a `:!<pattern>` exclude pathspec, appended to `git ls-files` after `--`.
    A caller vendoring a subtree it does not author, per GOVERNANCE.md's carry-versus-reach test,
    can scope every check out of that subtree this way. No check itself needs to change.
    Additive only: an empty or absent `exclude` scans exactly what it always has.

    Git's quoting is pinned on, so the listing is ASCII whatever a config inherits and each quoted
    name reaches `unquote_path` in the one form it decodes. Git's stderr carries no such quoting, and
    a failure naming a root that is not UTF-8 echoes that name raw, so the decode tolerates it.
    """
    args = ["git", "-C", str(root), "-c", "core.quotePath=true", "ls-files"]
    if exclude:
        args += ["--", *(f":!{pattern}" for pattern in exclude)]
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=False,
    )
    if result.returncode != 0:
        # A failed command's stdout is never trusted, even where it is non-empty.
        # A partial listing read as complete is a scan that missed files and said nothing.
        reason = result.stderr.strip() or f"exit {result.returncode}, no stderr"
        print(f"git ls-files failed: {printable(reason)}", file=sys.stderr)
        return []
    return [unquote_path(l) for l in result.stdout.split("\n") if l]


def unquote_path(name: str) -> str:
    """The real name behind one `git ls-files` line, which git quotes when the name needs it.

    Git quotes a name holding any byte at or above 0x80, and one holding a quote, a backslash,
    or a control character, escaping it the way C does. Read as a literal path, the quoted form
    names no file on disk, so a check reading the file would pass over it and report clean.

    The caller pins `core.quotePath=true`, which is what makes a quoted line ASCII and so what
    this decode assumes. Turning the setting off instead would not do: it stops git quoting the
    first of those three routes and leaves the other two quoting a name whose non-ASCII bytes sit
    raw inside the quotes, which this decode cannot carry.

    A byte that is not valid UTF-8 comes back as a surrogate escape, the form `Path` and
    `resolved_eol` both encode back to the original byte. Latin-1 carries each unescaped byte
    through unchanged on the way there.
    """
    if name.startswith('"') and name.endswith('"') and len(name) > 1:
        unescaped = name[1:-1].encode("latin-1", "backslashreplace").decode("unicode-escape")
        return unescaped.encode("latin-1", "surrogateescape").decode("utf-8", "surrogateescape")
    return name


def workflow_files(files: list[str]) -> list[str]:
    """The files sha-pin governs, exposed so a test can assert the scan found something.

    A scan that matches nothing reports `0 issue(s)`, indistinguishable from a clean tree.
    """
    return [f for f in files if WORKFLOW.search(f)]


def resolved_eol(root: Path, paths: list[str]) -> dict[str, str] | None:
    """What git resolves `eol` to for each path, or None where git did not answer at all.

    Asked of git rather than re-derived from `.gitattributes`, so this cannot disagree with what a
    checkout actually applies. NUL-delimited in both directions, since a tracked path may carry a
    space or a colon and the newline form splits those wrongly.
    """
    if not paths:
        return {}
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "check-attr", "-z", "--stdin", "eol"],
            input="\0".join(paths) + "\0",
            capture_output=True,
            text=True,
            encoding="utf-8",
            # `-z` turns off the quoting that keeps other git output ASCII, so this echoes a path back raw.
            # A strict decode raises a ValueError, which the handler below does not catch.
            errors="surrogateescape",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    f = r.stdout.split("\0")
    return {f[i]: f[i + 2] for i in range(0, len(f) - 2, 3)}


def check_sha_pin(root: Path, files: list[str]) -> list[str]:
    """Every external `uses:` is a 40-hex SHA, and one under this owner resolves.

    A local (`./`) or self-repository (`$/`) ref names no ref to pin and is skipped, once any
    surrounding YAML quotes are stripped. A ref starting with a bare `.github/` is reported,
    because GitHub reads it as `owner/repo` rather than as a path and it fails at run time.
    References under another owner are shape-checked but not resolved.

    Resolution is scoped to the scanned repository's own owner, because that is where the fleet's
    own actions live and where the decay this catches comes from: a squash merge deletes the
    branch a pin was taken from, and the pin outlives the commit. A third-party action's tag is
    stable by comparison, and reading one would make every local run of this gate depend on a
    stranger's repository answering. The cost is stated rather than left to be found, and it is
    real: a fabricated pin on a third-party action is still only shape-checked here. The counts
    below print on every run so that narrowness is visible rather than inferred from a clean line.
    """
    bad = []
    owner = origin_owner(root)
    cache: dict[tuple[str, str], bool | None] = {}
    resolved = foreign = unowned = unread = 0
    for rel in workflow_files(files):
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in USES.finditer(text):
            ref = m.group("ref")
            if len(ref) > 1 and ref[0] == ref[-1] and ref[0] in "\"'":
                ref = ref[1:-1]
            if ref.startswith(("$/", "./")):
                continue
            if ref.startswith(".github/"):
                line = text[: m.start()].count("\n") + 1
                bad.append(
                    f"{rel}:{line}: `uses: {ref}` names no local path without a leading `./`"
                )
                continue
            if "@" not in ref:
                bad.append(f"{rel}: `uses: {ref}` has no ref at all")
                continue
            action, _, ver = ref.rpartition("@")
            if action in SHA_EXCEPTIONS:
                continue
            line = text[: m.start()].count("\n") + 1
            if not PIN.match(ver):
                bad.append(f"{rel}:{line}: `{action}@{ver}` is a floating ref, not a 40-hex SHA")
                continue
            # An action reference is `owner/repo` with an optional path to the action within it.
            nwo = "/".join(action.split("/")[:2])
            # Counted apart from a known other owner, since the two are not the same state.
            # The note exists to describe the narrowing exactly, so it must not merge them.
            # A checkout with no readable origin skips every pin, this owner's own included.
            if owner is None:
                unowned += 1
                continue
            if nwo.split("/")[0].lower() != owner:
                foreign += 1
                continue
            state = pin_resolves(nwo, ver, cache)
            if state is False:
                bad.append(
                    f"{rel}:{line}: `{action}@{ver}` is shaped like a SHA and resolves to "
                    f"no commit in {nwo}"
                )
            elif state is None:
                unread += 1
            else:
                resolved += 1
    # Unconditional, so an all-zero run is as visible as a count rather than a clean line.
    # A guard on a non-zero counter hides the run that resolved nothing, which is this one.
    NOTES.append(
        f"resolved {resolved} pin(s) against GitHub. Read for shape only: "
        f"{foreign} under another owner, {unowned} whose owner could not be "
        f"compared because this checkout's origin is unreadable, "
        f"{unread} GitHub did not answer for."
    )
    return bad


def editorconfig_ending(text: str, header: str) -> str | None:
    """Return one section's declared ending without reading into the next section."""
    section = re.search(rf"(?ms)^\[{re.escape(header)}\]\s*$\n(?P<body>.*?)(?=^\[|\Z)", text)
    if section is None:
        return None
    ending = re.search(r"(?m)^end_of_line\s*=\s*(lf|crlf)\s*$", section.group("body"))
    return None if ending is None else ending.group(1)


def check_eol(root: Path, files: list[str]) -> list[str]:
    """The EditorConfig and Git defaults declare the same global and Windows endings."""
    ec, ga = root / ".editorconfig", root / ".gitattributes"
    missing = [
        name for name, p in ((".editorconfig", ec), (".gitattributes", ga)) if not p.exists()
    ]
    if missing:
        return [f"missing {name}" for name in missing]
    ec_text = ec.read_text(encoding="utf-8", errors="replace")
    ga_text = ga.read_text(encoding="utf-8", errors="replace")
    ec_global = editorconfig_ending(ec_text, "*")
    ga_global = re.search(r"(?m)^\*\s+text=auto\s+eol=(lf|crlf)\s*$", ga_text)
    out = []
    if ec_global is None:
        out.append(".editorconfig has no `[*]` end_of_line default")
    if ga_global is None:
        out.append(".gitattributes has no `* text=auto eol=<ending>` default")
    if ec_global and ga_global and ec_global != ga_global.group(1):
        out.append(
            f"global ending differs: .editorconfig is {ec_global}, "
            f".gitattributes is {ga_global.group(1)}"
        )
    section_headers = re.findall(r"(?m)^\[(?P<header>[^]]+)\]\s*$", ec_text)
    combined_windows = any(
        header.startswith("*.{")
        and header.endswith("}")
        and {"bat", "cmd"}.issubset(part.strip() for part in header[3:-1].split(","))
        and editorconfig_ending(ec_text, header) == "crlf"
        for header in section_headers
    )
    separate_windows = all(
        editorconfig_ending(ec_text, header) == "crlf" for header in ("*.bat", "*.cmd")
    )
    if not combined_windows and not separate_windows:
        out.append(".editorconfig does not set `*.bat` and `*.cmd` to CRLF")
    for pattern in ("*.bat", "*.cmd"):
        if not re.search(rf"(?m)^{re.escape(pattern)}\s+text\s+eol=crlf\s*$", ga_text):
            out.append(f".gitattributes does not set `{pattern}` to CRLF")
    NOTES.append("compared the global text default and both Windows command-script exceptions.")
    return out


def check_eol_coverage(root: Path, files: list[str]) -> list[str]:
    """Representative paths resolve through Git to the declared repository-wide policy."""
    ga = root / ".gitattributes"
    if not ga.exists():
        return ["missing .gitattributes"]
    ga_text = ga.read_text(encoding="utf-8", errors="replace")
    global_default = re.search(r"(?m)^\*\s+text=auto\s+eol=(lf|crlf)\s*$", ga_text)
    if global_default is None:
        return [".gitattributes has no `* text=auto eol=<ending>` default"]
    default_ending = global_default.group(1)
    paths = [
        "README.md",
        "script.py",
        "tool.sh",
        "tool",
        "uv.lock",
        "Dockerfile",
        ".github/workflows/check.yml",
        "script.bat",
        "script.cmd",
    ]
    attrs = resolved_eol(root, paths)
    if attrs is None:
        NOTES.append("git did not answer `check-attr`, so no representative path was read.")
        return []
    lf_override_paths = {"script.py", "tool.sh", "tool", "uv.lock"}
    expected = {
        path: ({"lf", default_ending} if path in lf_override_paths else {"crlf"})
        if path.endswith((".bat", ".cmd")) or default_ending == "crlf"
        else {"lf"}
        for path in paths
    }
    out = [
        f"{path}: git resolves to `eol: {attrs.get(path, 'unspecified')}`, "
        f"not one of `{', '.join(sorted(endings))}`"
        for path, endings in expected.items()
        if attrs.get(path) not in endings
    ]
    shebang_paths = []
    for rel in files:
        path = root / rel
        try:
            if path.is_file() and path.read_bytes()[:2] == b"#!":
                shebang_paths.append(rel)
        except OSError:
            continue
    shebang_attrs = resolved_eol(root, shebang_paths) if shebang_paths else {}
    if shebang_attrs is None:
        NOTES.append("git did not answer `check-attr`, so no tracked shebang path was read.")
    else:
        out.extend(
            f"{path}: tracked shebang path resolves to "
            f"`eol: {shebang_attrs.get(path, 'unspecified')}`, not `lf`"
            for path in shebang_paths
            if shebang_attrs.get(path) != "lf"
        )
    NOTES.append(f"resolved {len(paths)} representative path(s) through git check-attr.")
    NOTES.append(f"checked {len(shebang_paths)} tracked shebang path(s).")
    return out


ACTION_FILE = re.compile(r"^\.github/actions/(?:.+/)?action\.ya?ml$")
# A GitHub expression is not shell, so shellcheck would read its braces as syntax.
EXPRESSION_START = "${{"
SHELLCHECK_IMAGE = "koalaman/shellcheck:stable"
SHELLCHECK_TIMEOUT = 300
SCHEMA_TIMEOUT = 300
YAML_TIMEOUT = 60
DOCKER_TIMEOUT = 30


def action_files(files: list[str]) -> list[str]:
    return [f for f in files if ACTION_FILE.match(f)]


def single_document(documents: list[object]) -> object:
    if len(documents) != 1:
        raise ValueError(f"expected one YAML document, found {len(documents)}")
    return documents[0]


UVX_READER = (
    "import sys\n"
    "sys.path[:] = [p for p in sys.path if p]\n"
    "import json, yaml\n"
    "def step(item):\n"
    "    if not isinstance(item, dict):\n"
    "        return None\n"
    "    out = {'run': item['run']} if isinstance(item.get('run'), str) else {}\n"
    "    if item.get('name') and not isinstance(item['name'], (dict, list)):\n"
    "        out['name'] = str(item['name'])\n"
    "    if item.get('shell') is not None:\n"
    "        out['shell'] = item['shell'] if isinstance(item['shell'], str) else ''\n"
    "    return out\n"
    "def project(document):\n"
    "    if not isinstance(document, dict):\n"
    "        return None\n"
    "    runs = document.get('runs')\n"
    "    steps = runs.get('steps') if isinstance(runs, dict) else None\n"
    "    if not isinstance(steps, list):\n"
    "        return {}\n"
    "    return {'runs': {'steps': [step(item) for item in steps]}}\n"
    "try:\n"
    "    with open(sys.argv[1], encoding='utf-8') as handle:\n"
    "        text = json.dumps([project(document) for document in yaml.safe_load_all(handle)])\n"
    "except Exception as error:\n"
    "    if isinstance(error, yaml.YAMLError):\n"
    "        line = f'invalid YAML: {error}'\n"
    "    else:\n"
    "        line = f'unreadable: {type(error).__name__}: {error}'\n"
    "    sys.exit(' '.join(line.split()))\n"
    "sys.stdout.write(text)\n"
)
PYYAML_PYTHON: list[str] = []


class UvxUnavailable(Exception):
    """uvx could not provide an interpreter with PyYAML, so no action can be read through it."""


def run_utf8(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
        timeout=YAML_TIMEOUT,
    )


def pyyaml_python() -> str:
    """An interpreter that imports PyYAML, resolved through uvx once per run."""
    if PYYAML_PYTHON:
        return PYYAML_PYTHON[0]
    uvx = shutil.which("uvx")
    if uvx is None:
        raise UvxUnavailable("neither PyYAML nor uvx is available to read the action")
    probe = (
        "import sys; sys.path[:] = [p for p in sys.path if p]; import yaml; print(sys.executable)"
    )
    try:
        result = run_utf8([uvx, "--with", "pyyaml", "python", "-c", probe])
    except subprocess.TimeoutExpired:
        raise UvxUnavailable(f"uvx timed out after {YAML_TIMEOUT}s providing PyYAML") from None
    except OSError as error:
        raise UvxUnavailable(f"uvx could not start: {error}") from None
    if result.returncode != 0 or not result.stdout.strip():
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        first = f": {lines[0]}" if lines else ""
        raise UvxUnavailable(f"uvx exited {result.returncode} providing PyYAML{first}")
    PYYAML_PYTHON.append(result.stdout.strip())
    return PYYAML_PYTHON[0]


def load_through_uvx(path: Path) -> list[object]:
    """The fields the check reads from each document, parsed by PyYAML in the uvx interpreter."""
    python = pyyaml_python()
    try:
        result = run_utf8([python, "-c", UVX_READER, str(path)])
    except subprocess.TimeoutExpired:
        raise ValueError(f"reading timed out after {YAML_TIMEOUT}s") from None
    except OSError as error:
        raise UvxUnavailable(f"the interpreter uvx provided could not start: {error}") from None
    if result.returncode != 0:
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        raise ValueError(lines[-1] if lines else f"the reader exited {result.returncode}")
    documents: list[object] = json.loads(result.stdout)
    return documents


def load_action(path: Path) -> object:
    """The one parsed document of an action file, through PyYAML on every host.

    PyYAML is used in process where it is importable, and otherwise under uvx, a declared host tool.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return single_document(load_through_uvx(path))
    try:
        with path.open(encoding="utf-8") as handle:
            return single_document(list(yaml.safe_load_all(handle)))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid YAML: {error}") from error


def step_dialect(shell: object) -> str | None:
    """The shellcheck dialect for a step's `shell`, or None where the step is not bash or sh."""
    if shell is None:
        return "bash"
    words = str(shell).split()
    name = words[0].rsplit("/", 1)[-1] if words else ""
    return name if name in {"bash", "sh"} else None


def expression_end(body: str, start: int) -> int:
    """The index just past the `}}` closing the expression at `start`, quoted strings skipped.

    An expression whose `}}` comes after the next opener outside a string is unterminated too.
    An unterminated expression returns -1, so the caller leaves it as raw text for shellcheck.
    """
    at = start + len(EXPRESSION_START)
    quoted = False
    while at < len(body):
        char = body[at]
        if char == "'":
            quoted = not quoted
        elif not quoted and body.startswith("}}", at):
            return at + 2
        elif not quoted and body.startswith(EXPRESSION_START, at):
            return -1
        at += 1
    return -1


def mark_expressions(body: str) -> str:
    """Swap each expression for `${GHA_EXPR}` plus one NUL per newline it held."""
    out: list[str] = []
    at = 0
    while True:
        start = body.find(EXPRESSION_START, at)
        if start < 0:
            out.append(body[at:])
            return "".join(out)
        end = expression_end(body, start)
        if end < 0:
            out.append(body[at : start + len(EXPRESSION_START)])
            at = start + len(EXPRESSION_START)
            continue
        out.append(body[at:start] + "${GHA_EXPR}" + "\0" * body[start:end].count("\n"))
        at = end


def substitute_expressions(body: str) -> str:
    """Replace each expression with `${GHA_EXPR}`, keeping every later line at its own number.

    A multi-line expression collapses onto its first line.
    Its extra newlines return as empty lines after the end of that logical line.
    A backslash continuation would not hold inside a comment, and a physical line end would split a command.
    """
    marked = mark_expressions(body)
    out: list[str] = []
    pending = 0
    for line in marked.split("\n"):
        pending += line.count("\0")
        line = line.replace("\0", "")
        out.append(line)
        slashes = len(line) - len(line.rstrip("\\"))
        if pending and slashes % 2 == 0:
            out.extend([""] * pending)
            pending = 0
    return "\n".join(out)


def shellcheck_body(label: str, dialect: str, text: str) -> list[str]:
    # SC2154 is off because a step's `env:` variables are invisible to shellcheck.
    command = ["docker", "run", "--rm", "-i", "--network=none", SHELLCHECK_IMAGE]
    command += ["-s", dialect, "-S", "warning", "-e", "SC2154", "-f", "gcc", "-"]
    try:
        result = subprocess.run(
            command,
            input=text.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=SHELLCHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return [f"{label}: shellcheck timed out after {SHELLCHECK_TIMEOUT}s"]
    except OSError as error:
        return [f"{label}: could not start docker for shellcheck: {error}"]
    if result.returncode == 0:
        return []
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    lines = [line.removeprefix("-:") for line in stdout.splitlines() if line]
    if result.returncode == 1 and lines:
        return [f"{label}: run line {line}" for line in lines]
    return [f"{label}: shellcheck exited {result.returncode}: {stderr}"]


def docker_unreachable() -> str | None:
    """The reason no docker daemon answers or the image cannot be pulled, or None where both work.

    The explicit pull makes a fetch failure one finding with its reason, rather than one per body.
    A locally cached image is reused, so a local run may lag CI, which always pulls fresh.
    """
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, check=False, timeout=DOCKER_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return f"docker did not answer within {DOCKER_TIMEOUT}s"
    except OSError as error:
        return f"docker could not start: {error}"
    if result.returncode != 0:
        return f"docker info exited {result.returncode}"
    try:
        present = subprocess.run(
            ["docker", "image", "inspect", SHELLCHECK_IMAGE],
            capture_output=True,
            check=False,
            timeout=DOCKER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"docker image inspect did not finish within {DOCKER_TIMEOUT}s"
    except OSError as error:
        return f"docker image inspect could not start: {error}"
    if present.returncode == 0:
        return None
    try:
        pull = subprocess.run(
            ["docker", "pull", "--quiet", SHELLCHECK_IMAGE],
            capture_output=True,
            check=False,
            timeout=SHELLCHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"docker pull did not finish within {SHELLCHECK_TIMEOUT}s"
    except OSError as error:
        return f"docker pull could not start: {error}"
    if pull.returncode != 0:
        detail = pull.stderr.decode("utf-8", errors="replace").strip()
        return f"docker pull of {SHELLCHECK_IMAGE} exited {pull.returncode}: {detail}"
    return None


def check_composite_shell(root: Path, path: str) -> list[str]:
    try:
        action = load_action(root / path)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        return [f"{path}: could not read the action: {error}"]
    if not isinstance(action, dict):
        return [f"{path}: could not read the action: the document is not a mapping"]
    runs = action.get("runs")
    steps = runs.get("steps") if isinstance(runs, dict) else None
    if not isinstance(steps, list):
        return []
    hits: list[str] = []
    for index, step in enumerate(steps, start=1):
        body = step.get("run") if isinstance(step, dict) else None
        if not isinstance(body, str) or not body.strip():
            continue
        dialect = step_dialect(step.get("shell"))
        if dialect is None:
            continue
        label = f"{path} step {index}"
        if step.get("name"):
            label += f" ({step['name']})"
        hits.extend(shellcheck_body(label, dialect, substitute_expressions(body)))
    return hits


def schema_command() -> list[str] | None:
    args = ["check-jsonschema", "--builtin-schema", "vendor.github-actions", "--"]
    if shutil.which("uvx"):
        return ["uvx", "check-jsonschema@latest", *args[1:]]
    if shutil.which("pipx"):
        return ["pipx", "run", *args]
    return None


def check_composite_schema(root: Path, actions: list[str]) -> list[str]:
    command = schema_command()
    if command is None:
        return ["neither uvx nor pipx is installed, so no action was schema-checked"]
    try:
        result = subprocess.run(
            [*command, *actions],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=SCHEMA_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return [f"schema check timed out after {SCHEMA_TIMEOUT}s"]
    except OSError as error:
        return [f"schema check could not start: {error}"]
    if result.returncode == 0:
        return []
    detail = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    return [f"schema: {line}" for line in detail] or [f"schema: exited {result.returncode}"]


def check_composite_actions(root: Path, files: list[str]) -> list[str]:
    """Shellcheck every bash or sh `run:` body of a composite action and schema-check the file.

    actionlint reads workflows only, so a shell body moved into an action loses both checks.
    Each `${{ }}` expression becomes `${GHA_EXPR}`, which makes info and style findings unreliable.
    Bodies therefore run at warning severity, which is weaker than shellcheck over a `.sh` file.
    The uvx path of the schema half follows the uvx float policy that validate-task.yml states.
    Each half reports its own missing tool, and the other half still runs.
    """
    actions = [path for path in action_files(files) if (root / path).is_file()]
    if not actions:
        NOTES.append("no composite action file is tracked, so nothing was checked.")
        return []
    hits: list[str] = []
    unreachable = (
        "docker is not installed" if shutil.which("docker") is None else docker_unreachable()
    )
    if unreachable is not None:
        hits.append(f"{unreachable}, so no run body was shellchecked")
    else:
        PYYAML_PYTHON.clear()
        for path in actions:
            try:
                hits.extend(check_composite_shell(root, path))
            except UvxUnavailable as error:
                hits.append(f"{error}, so no run body was shellchecked")
                break
    hits.extend(check_composite_schema(root, actions))
    NOTES.append(f"checked {len(actions)} action file(s).")
    return hits


CHECKS = {
    "sha-pin": check_sha_pin,
    "eol": check_eol,
    "eol-coverage": check_eol_coverage,
    "composite-actions": check_composite_actions,
}


def printable(line: str) -> str:
    """`line` with each control character spelled as an escape, so one finding prints as one line.

    `unquote_path` hands back a tracked name exactly as it is on disk, and a name may hold a
    newline or an escape sequence. Printed raw, such a name forges a line of the gate's own output
    or drives the reader's terminal, so each C0 or C1 control and DEL is escaped here, where it is
    shown. A byte that is not UTF-8 arrives as a lone surrogate, which a strict stream refuses to
    encode, so it is spelled as an escape too, leaving the result safe on any stream.
    """
    escaped = CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", line)
    return escaped.encode("utf-8", "backslashreplace").decode("utf-8")


def report_paths_that_are_not_utf8() -> None:
    """Let a path holding a byte that is not UTF-8 print rather than ending the run.

    `unquote_path` decodes such a name with surrogateescape so it opens on disk, which leaves the
    lone surrogate in the name to reach this program's own output. Encoding it strictly raises at
    the line printing that name, so every check after it is lost along with the run's verdict, and
    the exit code becomes a traceback's rather than the gate's. Escaping it costs the reader one
    unreadable byte in one name.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")


def main(argv: list[str] | None = None) -> int:
    report_paths_that_are_not_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--check", action="append", choices=sorted(CHECKS))
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="git pathspec pattern to exclude from every check's tracked-file scan "
        "(for example 'themes/PaperMod/**'); repeatable",
    )
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    files = tracked(root, a.exclude)
    if not files:
        # `tracked()` already printed git's own stderr above where the command itself failed.
        # This distinguishes that root cause from a caller's exclude matching every tracked file.
        if a.exclude:
            print(
                f"{root}: no tracked files remain after excluding "
                f"{', '.join(a.exclude)} (or the ls-files command above failed)",
                file=sys.stderr,
            )
        else:
            print(f"{root}: not a git repo or no tracked files", file=sys.stderr)
        return 2
    if a.exclude:
        # Compared against the unfiltered scan.
        # A pattern matching nothing tracked is not reported as narrowing, which would misreport a caller's own typo as having worked.
        excluded = len(tracked(root)) - len(files)
        if excluded > 0:
            print(
                f"note: {len(a.exclude)} exclude pattern(s) narrowed the tracked-file scan "
                f"by {excluded} file(s): {', '.join(a.exclude)}"
            )
        else:
            print(
                f"note: {len(a.exclude)} exclude pattern(s) matched no tracked file, so "
                f"nothing was narrowed: {', '.join(a.exclude)}"
            )

    total = 0
    for name in a.check or sorted(CHECKS):
        # Cleared per check, so a note is attributed to the check that raised it.
        NOTES.clear()
        hits = CHECKS[name](root, files)
        status = "FAIL" if hits else "ok"
        print(f"[{status:4}] {name:12} {len(hits)} issue(s)")
        for h in hits:
            print(f"         {printable(h)}")
        # After the findings and outside the count, since a note is not one.
        for note in NOTES:
            print(f"         note: {printable(note)}")
        total += len(hits)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())

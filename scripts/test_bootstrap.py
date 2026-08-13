"""Tests for the host bootstraps: the loader invariant, and that the tooling covers what the spec requires.

Two properties, both of which fail silently rather than loudly if nobody checks them.

The loader invariant is what keeps `host-setup/bootstrap.sh` and `host-setup/bootstrap.ps1` outside
the reach of the `Hub-Hosted Tooling` rule rather than exempt from it. A loader obtains a tree and
hands control to one entry point inside it. The moment it reads a second path in that tree it has
become a tool that reads hub content, and the rule applies to it in full. That boundary is a property
of each file, so it is asserted here rather than promised in prose.

The coverage assertion is the only connection between the floors in `spec/host-tools.json` and the
tooling that installs them. Nothing joins the two at runtime, deliberately: the gate measures a host
and the tooling changes one, and neither calls the other. Without a check at this level a tool could
be declared required and be one nothing here can install, which a host would discover as a gate it
cannot satisfy.

That assertion runs once per platform, because the two installers do not manage the same set and the
difference is a decision rather than an accident. `git-restore-mtime` serves a Linux deploy path and
the spec declares it not applicable on Windows. `docker` is the reverse: one winget package there,
and on Linux an answer that differs by whether the host is a hypervisor, a WSL distribution, or a
workstation.

Run: python3 scripts/test_bootstrap.py
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / 'host-setup' / 'bootstrap.sh'
BOOTSTRAP_PS = ROOT / 'host-setup' / 'bootstrap.ps1'
LINUX = ROOT / 'host-setup' / 'linux'
WINDOWS = ROOT / 'host-setup' / 'windows'
HOST_TOOLS = ROOT / 'spec' / 'host-tools.json'

# The tools the linux tooling manages, read from the script rather than restated here, so the two cannot drift while both look correct.
TOOLS_DECLARATION = re.compile(r'^readonly TOOLS=\(([^)]*)\)', re.MULTILINE)

# The same, for the windows tooling, whose registry is a list of records rather than a flat array.
# The names are read out of the records themselves rather than from a second list beside them, so there is one declaration to keep true rather than two that can agree wrongly.
PS_TOOLS_OPEN = re.compile(r'^\$TOOLS\s*=\s*@\(', re.MULTILINE)
PS_TOOLS_CLOSE = re.compile(r'^\)', re.MULTILINE)
PS_TOOL_NAME = re.compile(r"^\s*@\{\s*Name\s*=\s*'([^']+)'", re.MULTILINE)

# A spec tool whose name differs from the name the installer knows it by, and why.
ALIASES = {
    'linux': {'python3': 'python'},
    'windows': {'python3': 'python'},
}

# A spec tool an installer deliberately does not manage, and the reason, recorded so an omission is a decision somebody made rather than one nobody noticed.
# The windows set is empty rather than absent, which is itself the assertion: Docker Desktop is one winget package there, where on Linux a hypervisor and a workstation want different answers, so an entry appearing here later is a decision to justify rather than a gap to fill.
NOT_MANAGED = {
    'linux': {
        'docker': 'Installed from the vendor script per the distribution, and a hypervisor or a WSL '
                  'distribution wants a different answer than a workstation does.',
    },
    'windows': {},
}

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def declared_tools() -> set[str]:
    """The tool names `install-tools.sh` manages."""
    text = (LINUX / 'install-tools.sh').read_text(encoding='utf-8')
    match = TOOLS_DECLARATION.search(text)
    if not match:
        failures.append('install-tools.sh declares no TOOLS array, so coverage cannot be checked')
        return set()
    return {name.strip() for name in match.group(1).split() if name.strip()}


def declared_windows_tools() -> set[str]:
    """The tool names `install-tools.ps1` manages."""
    text = (WINDOWS / 'install-tools.ps1').read_text(encoding='utf-8')
    opened = PS_TOOLS_OPEN.search(text)
    if not opened:
        failures.append('install-tools.ps1 declares no $TOOLS registry, so coverage cannot be checked')
        return set()

    # A missing close marker is a failure rather than a scan to end of file.
    # Reading on past the registry collects every later `Name = '...'` in the script, so a broken registry answers with a larger tool set than it declares and the coverage check below passes on it.
    closed = PS_TOOLS_CLOSE.search(text, opened.end())
    if not closed:
        failures.append('install-tools.ps1 opens a $TOOLS registry this cannot find the end of, so coverage cannot be checked')
        return set()

    body = text[opened.end():closed.start()]
    names = set(PS_TOOL_NAME.findall(body))
    if not names:
        failures.append('install-tools.ps1 declares a $TOOLS registry with no Name fields this can read')
    return names


def spec_tools() -> list[dict]:
    """The tools the spec declares, or an empty list and a recorded failure."""
    # A malformed or unreadable spec is a finding this file reports beside the others, rather than a traceback that ends the run and takes the checks after it with it.
    try:
        return json.loads(HOST_TOOLS.read_text(encoding='utf-8'))['tools']
    except (OSError, ValueError, KeyError) as error:
        failures.append(f'{HOST_TOOLS.name} could not be read as a tool declaration: {error}')
        return []


def assert_loader_reads_one_path(path: Path, expected: str) -> None:
    """A loader references exactly one directory inside the tree it fetches.

    Both loaders write that one reference as a single interpolated, forward-slashed string
    (`$TREE/host-setup/<platform>/$tool` in each), rather than building it from parts, which is what
    lets this one pattern read either file unmodified.
    """
    text = path.read_text(encoding='utf-8')

    # Every reference to the fetched tree goes through the variable holding its location, so the paths it names are countable rather than scattered.
    references = re.findall(r'\$TREE(?:/[^"\'\s]*)?', text)
    paths = {reference for reference in references if '/' in reference}

    check(
        paths == {expected},
        f'{path.name} reads more than its one entry point into the fetched tree: {sorted(paths)}',
    )

    # A payload or a table read from the tree is what makes a file a tool rather than a loader.
    for forbidden in ('spec/', 'registry/', 'repo-config/', 'catalog/'):
        check(
            f'$TREE/{forbidden}' not in text,
            f'{path.name} reads {forbidden} from the fetched tree, which makes it a tool',
        )


def assert_loader_needs_no_python(path: Path) -> None:
    """A host being bootstrapped must not be made to install an interpreter first."""
    text = path.read_text(encoding='utf-8')
    for interpreter in ('python3 ', 'python ', 'uv run', 'py -3'):
        check(
            interpreter not in text,
            f'{path.name} invokes {interpreter.strip()}, which a host being bootstrapped may not have',
        )


def test_linux_loader_reads_one_path_into_the_tree() -> None:
    """`bootstrap.sh` references exactly one directory inside the tree it fetches."""
    assert_loader_reads_one_path(BOOTSTRAP, '$TREE/host-setup/linux/$tool')


def test_windows_loader_reads_one_path_into_the_tree() -> None:
    """`bootstrap.ps1` references exactly one directory inside the tree it fetches.

    `$Tool`, PascalCase, because that is the parameter name PowerShell convention wants, where bash's
    equivalent is the lowercase local `$tool`. The pattern this shares with the Linux check is the shape
    of the path, one interpolated `$TREE/host-setup/<platform>/<name>` string, not the exact casing.
    """
    assert_loader_reads_one_path(BOOTSTRAP_PS, '$TREE/host-setup/windows/$Tool')


def test_linux_loader_needs_no_python() -> None:
    """A host `bootstrap.sh` stands up must not be made to install an interpreter first."""
    assert_loader_needs_no_python(BOOTSTRAP)


def test_windows_loader_needs_no_python() -> None:
    """A host `bootstrap.ps1` stands up must not be made to install an interpreter first."""
    assert_loader_needs_no_python(BOOTSTRAP_PS)


def assert_coverage(platform: str, managed: set[str], installer: str) -> None:
    """A tool the spec requires is one the named installer can provide, or a recorded exception."""
    if not managed:
        return

    for tool in spec_tools():
        name = tool['name']
        if not tool.get('required', False):
            continue

        # Every required tool is checked, including one whose declaration names no source for this platform.
        # A tool is in scope because the spec requires it, never because its declaration happens to describe where this platform gets it.
        # Reading a missing `source.linux` as "not a Linux tool" skipped docker, git and uv, which is half the required set and the whole of what NOT_MANAGED exists to record.
        expected = ALIASES[platform].get(name, name)
        if name in NOT_MANAGED[platform]:
            check(
                expected not in managed,
                f'{name} is recorded as not managed on {platform}, but {installer} manages it, so the record is stale',
            )
            continue

        check(
            expected in managed,
            f'the spec requires {name} on {platform} and {installer} does not manage it, '
            f'so a host cannot satisfy the gate by running the tooling',
        )


def test_every_required_linux_tool_is_installable() -> None:
    """A tool the spec requires on Linux is one the tooling can provide, or a recorded exception."""
    assert_coverage('linux', declared_tools(), 'install-tools.sh')


def test_every_required_windows_tool_is_installable() -> None:
    """A tool the spec requires on Windows is one the tooling can provide, or a recorded exception."""
    assert_coverage('windows', declared_windows_tools(), 'install-tools.ps1')


def indexed_modes() -> dict[str, str]:
    """The file modes git records, keyed by repo-relative path.

    Git's mode is read rather than the filesystem's, because the filesystem does not carry one on
    every platform this runs on. NTFS has no exec bit, so `st_mode` reports every file as
    non-executable and the assertion below fails on Windows against a tree that is correct. What the
    loader actually depends on is the mode a Linux checkout gets, and that is the one git stores.
    """
    try:
        listing = subprocess.run(
            ['git', 'ls-files', '-s', '--', 'host-setup'],
            capture_output=True, text=True, check=True, cwd=ROOT,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        failures.append(f'git could not report the recorded file modes, so executability is unchecked: {error}')
        return {}

    modes: dict[str, str] = {}
    for line in listing.splitlines():
        # Each row is "<mode> <object> <stage>\t<path>", so the tab is what separates the fields from the path.
        fields, _, path = line.partition('\t')
        if path:
            modes[path] = fields.split()[0]
    return modes


def test_every_managed_tool_is_executable() -> None:
    """Each script the loader hands control to is present and executable."""
    modes = indexed_modes()
    for name in ('install-tools.sh', 'upgrade-host.sh', 'setup-github.sh'):
        path = LINUX / name
        check(path.is_file(), f'{name} is missing from host-setup/linux')
        if path.is_file() and modes:
            mode = modes.get(f'host-setup/linux/{name}', '')
            check(
                mode == '100755',
                f'{name} is recorded as {mode or "untracked"} rather than 100755, '
                f'so a fresh checkout cannot run it',
            )


def test_every_windows_script_is_present() -> None:
    """Each Windows script is present, and none opens with a shebang.

    The exec bit is the Linux form of "this will run", and on Windows the equivalent property is the
    absence of a shebang: `scripts/repo_gate.py --check eol-coverage` requires git to resolve any
    tracked file opening `#!` to `eol=lf`, and these files are CRLF by the `[*]` .editorconfig
    default with no pin of their own. A shebang added later would fail that gate from a file nobody
    would think to look at.
    """
    scripts = ('install-tools.ps1', 'upgrade-host.ps1', 'setup-github.ps1', 'setup-wsl.ps1')
    for name in scripts + ('README.md',):
        check((WINDOWS / name).is_file(), f'{name} is missing from host-setup/windows')

    for name in scripts:
        path = WINDOWS / name
        if path.is_file():
            check(
                not path.read_bytes().startswith(b'#!'),
                f'{name} opens with a shebang, which the eol-coverage gate then pins to LF, '
                f'against the CRLF these files are written with',
            )


def test_bootstrap_ps1_is_present_and_unmarked() -> None:
    """`bootstrap.ps1` is present, and does not open with a shebang.

    Kept apart from `test_every_windows_script_is_present` rather than folded into it, because
    `bootstrap.ps1` deliberately sits beside `bootstrap.sh` at `host-setup/`, not inside
    `host-setup/windows/` with the four scripts that test checks. Same reasoning as that test: the
    `eol-coverage` gate pins a tracked file opening `#!` to `eol=lf`, against the CRLF this file is
    written with.
    """
    check(BOOTSTRAP_PS.is_file(), 'bootstrap.ps1 is missing from host-setup')
    if BOOTSTRAP_PS.is_file():
        check(
            not BOOTSTRAP_PS.read_bytes().startswith(b'#!'),
            'bootstrap.ps1 opens with a shebang, which the eol-coverage gate then pins to LF, '
            'against the CRLF this file is written with',
        )


def main() -> int:
    for test in (
        test_linux_loader_reads_one_path_into_the_tree,
        test_windows_loader_reads_one_path_into_the_tree,
        test_linux_loader_needs_no_python,
        test_windows_loader_needs_no_python,
        test_every_required_linux_tool_is_installable,
        test_every_required_windows_tool_is_installable,
        test_every_managed_tool_is_executable,
        test_every_windows_script_is_present,
        test_bootstrap_ps1_is_present_and_unmarked,
    ):
        test()

    if failures:
        print(f'[FAIL] bootstrap  {len(failures)} issue(s)')
        for failure in failures:
            print(f'         {failure}')
        return 1

    print('[ OK ] bootstrap  loader invariant and spec coverage')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

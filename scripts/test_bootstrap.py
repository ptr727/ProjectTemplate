"""Tests for the host bootstrap: the loader invariant, and that the tooling covers what the spec requires.

Two properties, both of which fail silently rather than loudly if nobody checks them.

The loader invariant is what keeps `host-setup/bootstrap.sh` outside the reach of the
`Hub-Hosted Tooling` rule rather than exempt from it. A loader obtains a tree and hands control to
one entry point inside it. The moment it reads a second path in that tree it has become a tool that
reads hub content, and the rule applies to it in full. That boundary is a property of the file, so
it is asserted here rather than promised in prose.

The coverage assertion is the only connection between the floors in `spec/host-tools.json` and the
tooling that installs them. Nothing joins the two at runtime, deliberately: the gate measures a host
and the tooling changes one, and neither calls the other. Without a check at this level a tool could
be declared required and be one nothing here can install, which a host would discover as a gate it
cannot satisfy.

Run: python3 scripts/test_bootstrap.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / 'host-setup' / 'bootstrap.sh'
LINUX = ROOT / 'host-setup' / 'linux'
HOST_TOOLS = ROOT / 'spec' / 'host-tools.json'

# The tools the linux tooling manages, read from the script rather than restated here, so the two cannot drift while both look correct.
TOOLS_DECLARATION = re.compile(r'^readonly TOOLS=\(([^)]*)\)', re.MULTILINE)

# A spec tool whose name differs from the name the installer knows it by, and why.
ALIASES = {
    'python3': 'python',
}

# A spec tool the installer deliberately does not manage, and the reason, recorded so an omission is a decision somebody made rather than one nobody noticed.
NOT_MANAGED = {
    'docker': 'Installed from the vendor script per the distribution, and a hypervisor or a WSL '
              'distribution wants a different answer than a workstation does.',
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


def spec_tools() -> list[dict]:
    """The tools the spec declares, or an empty list and a recorded failure."""
    # A malformed or unreadable spec is a finding this file reports beside the others, rather than a traceback that ends the run and takes the checks after it with it.
    try:
        return json.loads(HOST_TOOLS.read_text(encoding='utf-8'))['tools']
    except (OSError, ValueError, KeyError) as error:
        failures.append(f'{HOST_TOOLS.name} could not be read as a tool declaration: {error}')
        return []


def test_loader_reads_one_path_into_the_tree() -> None:
    """The loader references exactly one directory inside the tree it fetches."""
    text = BOOTSTRAP.read_text(encoding='utf-8')

    # Every reference to the fetched tree goes through the variable holding its location, so the paths it names are countable rather than scattered.
    references = re.findall(r'\$TREE(?:/[^"\'\s]*)?', text)
    paths = {reference for reference in references if '/' in reference}

    check(
        paths == {'$TREE/host-setup/linux/$tool'},
        f'the loader reads more than its one entry point into the fetched tree: {sorted(paths)}',
    )

    # A payload or a table read from the tree is what makes a file a tool rather than a loader.
    for forbidden in ('spec/', 'registry/', 'repo-config/', 'catalog/'):
        check(
            f'$TREE/{forbidden}' not in text,
            f'the loader reads {forbidden} from the fetched tree, which makes it a tool',
        )


def test_loader_needs_no_python() -> None:
    """A host being bootstrapped must not be made to install an interpreter first."""
    text = BOOTSTRAP.read_text(encoding='utf-8')
    for interpreter in ('python3 ', 'python ', 'uv run', 'py -3'):
        check(
            interpreter not in text,
            f'the loader invokes {interpreter.strip()}, which a host being bootstrapped may not have',
        )


def test_every_required_linux_tool_is_installable() -> None:
    """A tool the spec requires on Linux is one the tooling can provide, or a recorded exception."""
    managed = declared_tools()
    if not managed:
        return

    for tool in spec_tools():
        name = tool['name']
        if not tool.get('required', False):
            continue

        # Every required tool is checked, including one whose declaration names no Linux source.
        # A tool is in scope because the spec requires it, never because its declaration happens to describe where Linux gets it.
        # Reading a missing `source.linux` as "not a Linux tool" skipped docker, git and uv, which is half the required set and the whole of what NOT_MANAGED exists to record.
        expected = ALIASES.get(name, name)
        if name in NOT_MANAGED:
            check(
                expected not in managed,
                f'{name} is recorded as not managed, but install-tools.sh manages it, so the record is stale',
            )
            continue

        check(
            expected in managed,
            f'the spec requires {name} on Linux and install-tools.sh does not manage it, '
            f'so a host cannot satisfy the gate by running the tooling',
        )


def test_every_managed_tool_is_executable() -> None:
    """Each script the loader hands control to is present and executable."""
    for name in ('install-tools.sh', 'upgrade-host.sh', 'setup-github.sh'):
        path = LINUX / name
        check(path.is_file(), f'{name} is missing from host-setup/linux')
        if path.is_file():
            check(path.stat().st_mode & 0o111 != 0, f'{name} is not executable, so the loader cannot run it')


def main() -> int:
    for test in (
        test_loader_reads_one_path_into_the_tree,
        test_loader_needs_no_python,
        test_every_required_linux_tool_is_installable,
        test_every_managed_tool_is_executable,
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

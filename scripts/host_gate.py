#!/usr/bin/env python3
"""Host tool versions against the floors in spec/host-tools.json - one command, one digest.

Why this exists: the verification block in docs/host-setup.md ran `--version` on each tool and read
only whether it answered. Presence is the weaker half of the contract, because the two host defects
this fleet has actually hit are both version facts that a present, working binary reports cleanly.

A stale `gh` is the first. The GitHub CLI maintainers name the community-distributed 2.45.x and
2.46.x as broken by deprecated GitHub APIs, and a host carrying one answers `gh --version` perfectly
while `gh pr edit` exits non-zero having applied nothing.

An old `git-restore-mtime` is the second, and it is worse, because it fails silently in the
direction that reads as success: a release before 2025.08 calls `git whatchanged`, current git
refuses it, and the tool then prints its ordinary statistics and exits 0 having restored nothing.

Both are invisible to a presence check and both are one comparison away from being caught, which is
the whole argument for this file. See docs/host-setup.md for the contract this reads and
GOVERNANCE.md "Hub-Hosted Tooling" for how a repository reaches it.

Exit 0 = every required tool is present and at or above its floor. 1 = at least one is missing or
below. 2 = the declaration itself could not be read, which is a defect here rather than on the host.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from pathlib import Path

SPEC = Path(__file__).resolve().parent.parent / 'spec' / 'host-tools.json'
NOTES: list[str] = []


def parse_version(text: str) -> tuple[int, ...] | None:
    """Dot-separated integers as a tuple, or None where the text is not one."""
    if not re.fullmatch(r'\d+(\.\d+)*', text or ''):
        return None
    return tuple(int(p) for p in text.split('.'))


def compare(found: tuple[int, ...], floor: tuple[int, ...]) -> int:
    """Sign of found minus floor, comparing element-wise with the shorter padded by zeros.

    Padding rather than comparing lengths is what lets a two-part `2025.08` sit against a three-part
    `2025.8.0` without either reading as the larger for having more components.
    """
    width = max(len(found), len(floor))
    a = found + (0,) * (width - len(found))
    b = floor + (0,) * (width - len(floor))
    return (a > b) - (a < b)


def probe(argv: list[str]) -> str | None:
    """The combined output of one probe, or None where the probe did not answer.

    stderr is joined in deliberately, since a version banner on stderr is as good an answer as one on
    stdout and reading only stdout would report a working tool as unreadable.

    A non-zero exit is not an answer, and reading it as one is a mistake this made on its first run:
    `git restore-mtime --version` on a host without the tool prints git's own "not a git command"
    text and exits 1, which counted as the probe having run and reported the tool as unreadable
    rather than absent. Those two have opposite remedies, fix this file against install the tool, so
    the exit code decides and a failing probe falls through to the next one.
    """
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return f'{p.stdout}\n{p.stderr}'


def read_tool(tool: dict) -> tuple[str, str | None, str | None]:
    """One tool's state as (status, version, detail).

    Status is `absent` where no probe ran, `unreadable` where one ran but the pattern did not match,
    and `read` otherwise. The three are kept apart because they have different remedies: install it,
    fix this file's pattern, or act on the version.
    """
    # The probe that answered is remembered rather than assumed to be the first one declared.
    # Naming a probe that never ran sends the reader to fix a pattern against output they cannot reproduce.
    answered = None
    for argv in tool['probes']:
        out = probe(argv)
        if out is None:
            continue
        if answered is None:
            answered = ' '.join(argv)
        m = re.search(tool['pattern'], out)
        if m:
            return 'read', m.group(1), ' '.join(argv)
    if answered is None:
        return 'absent', None, None
    return 'unreadable', None, answered


REQUIRED_FIELDS = ('name', 'probes', 'pattern', 'minimum', 'why')


def merge(base: list[dict], local: list[dict]) -> tuple[list[dict], list[str]]:
    """The hub declaration with a repository's own layered over it, plus any rejected overrides.

    A repository states what the fleet cannot know: that a tool the hub calls optional is mandatory
    there, that it needs a floor the hub has no opinion on, or that it depends on a tool no other
    repository has. Adding a new tool needs the same fields the hub's own entries carry, since a
    local entry the gate cannot probe is worth less than no entry.

    Overriding is **tighten-only**. A local entry may raise a floor, add one where the hub declares
    none, and turn an optional tool required. It may not lower a floor, remove one, or turn a
    required tool optional, because those are the edits that quietly retire a fleet check from inside
    the repository it was written to protect. A rejected override is reported rather than dropped,
    so a repository that tried to relax a floor hears about it instead of appearing to succeed.

    Names match without regard to case, so a repository writing `GH` overrides the hub's `gh` rather
    than silently adding a second entry beside it. Keying on the exact spelling would turn a
    misspelled override into an addition, which is the one outcome the author could not have
    intended and the gate would report as success.
    """
    by_key = {t['name'].lower(): dict(t) for t in base}
    rejected = []
    for entry in local:
        name = entry.get('name')
        if not isinstance(name, str) or not name:
            rejected.append('a local entry carries no name, so it was ignored')
            continue
        key = name.lower()
        if key not in by_key:
            missing = [f for f in REQUIRED_FIELDS if f not in entry]
            if missing:
                rejected.append(f'local tool {name} adds a new entry without {", ".join(missing)}, so it was ignored')
            else:
                by_key[key] = dict(entry)
            continue

        merged = by_key[key]
        for field, value in entry.items():
            # The hub's spelling of the name stands, since the gate reports under it and a local case variant is an override rather than a rename.
            if field == 'name':
                continue
            if field == 'required' and merged.get('required', True) and value is False:
                rejected.append(f'local tool {name} tried to turn a required tool optional, which is a relaxation, so the hub value stands')
                continue
            if field == 'minimum':
                hub_floor = parse_version(merged['minimum']) if merged['minimum'] else None
                new_floor = parse_version(value) if value else None
                if hub_floor is not None and (new_floor is None or compare(new_floor, hub_floor) < 0):
                    rejected.append(f'local tool {name} tried to lower the floor from {merged["minimum"]} to {value!r}, which is a relaxation, so the hub floor stands')
                    continue
            merged[field] = value
    return [by_key[k] for k in sorted(by_key)], rejected


def check(tools: list[dict]) -> list[str]:
    """Every declared tool against its floor, returning one line per failure."""
    issues = []
    for tool in tools:
        name = tool['name']
        required = tool.get('required', True)
        status, version, how = read_tool(tool)

        if status == 'absent':
            if required:
                issues.append(f'{name} is not installed, and it is required')
            else:
                NOTES.append(f'{name} is absent and optional, so nothing was read for it')
            continue

        if status == 'unreadable' or version is None:
            # Not a host failure, since the tool answered and the declaration failed to read it, so the remedy is in the declaration.
            issues.append(f'{name} ran via `{how}` but its declared pattern read no version from the output, so its floor was not applied')
            continue

        floor_text = tool['minimum']
        if floor_text is None:
            NOTES.append(f'{name} {version} present, and no floor is declared for it')
            continue

        found, floor = parse_version(version), parse_version(floor_text)
        if floor is None:
            # A malformed floor is a defect in the declaration, and silently skipping it would retire the check.
            issues.append(f'{name} declares the floor {floor_text!r}, which is not dot-separated integers, so nothing could be compared against it')
        elif found is None:
            issues.append(f'{name} reported {version!r}, which is not dot-separated integers, so its floor was not applied')
        elif compare(found, floor) < 0:
            # The remedy rides on the finding rather than beside it, since a separate line would count as a second issue.
            src = (tool.get('source') or {}).get('linux' if sys.platform.startswith('linux') else 'macos' if sys.platform == 'darwin' else 'windows')
            issues.append(f'{name} {version} is below the {floor_text} floor - {tool["why"]}'
                          + (f'\nINSTALL FROM: {src}' if src else ''))
        else:
            NOTES.append(f'{name} {version} meets the {floor_text} floor')
    return issues


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Check host tool versions against the hub declaration plus a repository\'s own.')
    ap.add_argument('--spec', default=str(SPEC), help='path to the hub declaration, for testing')
    ap.add_argument('--repo', default='.', help='repository whose host-tools.json layers over the hub one')
    ap.add_argument('--no-local', action='store_true', help='read the hub declaration alone, ignoring the repository')
    ap.add_argument('--quiet', action='store_true', help='print failures only, dropping the per-tool notes')
    a = ap.parse_args(argv)

    try:
        data = json.loads(Path(a.spec).read_text(encoding='utf-8'))
        tools = data['tools']
    except (OSError, ValueError, KeyError) as e:
        print(f'{a.spec}: cannot read the host tool declaration ({e})', file=sys.stderr)
        return 2

    NOTES.clear()
    rejected: list[str] = []
    local_path = Path(a.repo) / 'host-tools.json'
    # A repository layering onto the hub is the normal case, and carrying no local file is the common one, so its absence is silent.
    if not a.no_local and local_path.is_file():
        try:
            local = json.loads(local_path.read_text(encoding='utf-8'))['tools']
        except (OSError, ValueError, KeyError) as e:
            print(f'{local_path}: cannot read the repository host tool declaration ({e})', file=sys.stderr)
            return 2
        tools, rejected = merge(tools, local)
        NOTES.append(f'{local_path} layered {len(local)} local entry(s) over the hub declaration')

    issues = rejected + check(tools)
    status = 'FAIL' if issues else 'ok'
    print(f'[{status:4}] host-tools  {len(issues)} issue(s) over {len(tools)} declared tool(s)')
    for i in issues:
        # A finding may carry a continuation line, which is indented under it rather than counted beside it.
        head, _, tail = i.partition('\n')
        print(f'         {head}')
        if tail:
            print(f'           {tail}')
    if not a.quiet:
        # After the findings and outside the count, since a note is not one.
        for note in NOTES:
            print(f'         note: {note}')
    return 1 if issues else 0


if __name__ == '__main__':
    sys.exit(main())

"""Read one named step out of a workflow or composite action, bounded to that step.

The suites run on the standard library alone, so the readers work by indentation. A step is
sliced out before any key is looked for in it, so a step missing a key reads as missing rather
than as the next step's key.
"""

from __future__ import annotations

import re
from pathlib import Path


def step_script_text(text: str, name: str) -> str:
    """The `run` block of the step called `name` in `text`, dedented as a runner reads it.

    The `run:` key is looked for only at the step's own key level, so a step without one fails
    here rather than reading the next step's script.
    """
    step = yaml_step(text, name)
    key_level = len(step) - len(step.lstrip())
    match = re.search(rf"(?m)^ {{{key_level}}}run: \|[-+]?\n", step)
    if match is None:
        raise AssertionError(f"no run block in the step named {name!r}")
    indent = " " * (key_level + 2)
    script_lines = []
    for line in step[match.end() :].splitlines():
        if line.startswith(indent):
            script_lines.append(line.removeprefix(indent))
        elif not line:
            script_lines.append(line)
        else:
            break
    return "\n".join(script_lines)


def step_script(path: Path, name: str) -> str:
    """The `run` block of the step called `name` in the workflow or action at `path`."""
    return step_script_text(path.read_text(encoding="utf-8"), name)


def yaml_step(text: str, name: str) -> str:
    """The one sequence item named `name`, its dash blanked so its keys read as a mapping.

    Read by indentation, as `spec/audit.py`'s workflow readers are, since the suite runs on the
    standard library alone. The item ends at the first line at or left of its dash, so a key under
    a sibling step is never this step's.
    """
    lines = text.splitlines()
    starts = [
        i for i, line in enumerate(lines) if re.fullmatch(rf"( *)- name: {re.escape(name)}", line)
    ]
    if len(starts) != 1:
        raise AssertionError(f"{len(starts)} steps named {name!r}")
    first = lines[starts[0]]
    dash = len(first) - len(first.lstrip())
    item = [first[:dash] + "  " + first[dash + 2 :]]
    for line in lines[starts[0] + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= dash:
            break
        item.append(line)
    return "\n".join(item)


def yaml_value(text: str, *keys: str) -> str | None:
    """The text after the last key's colon at the nested mapping path `keys`, or None where absent.

    The text is raw rather than a parsed value, so quotes and a trailing comment are kept.

    Each key is looked for only among its parent's direct children, the shallowest lines under it,
    and a comment line is no key at all, so a commented-out or relocated entry reads as absent.
    """
    lines = [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    value = None
    for key in keys:
        if not lines:
            return None
        level = min(len(line) - len(line.lstrip()) for line in lines)
        found = None
        for i, line in enumerate(lines):
            if len(line) - len(line.lstrip()) == level and line[level:].startswith(f"{key}:"):
                found = i
                break
        if found is None:
            return None
        value = lines[found][level + len(key) + 1 :].strip()
        children = []
        for line in lines[found + 1 :]:
            if len(line) - len(line.lstrip()) <= level:
                break
            children.append(line)
        lines = children
    return value

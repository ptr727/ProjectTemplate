"""Which output lines the GitHub Actions runner would read as a workflow command.

Modeled on the runner's own two parsers rather than on the escaping a gate applies, so a test
asserting through it measures the runner's reading of the output rather than the gate's intent.
The current form opens on `::` once leading whitespace is trimmed. The legacy form is `##[`
anywhere in the line.

`FORGING_NAMES` holds constructed names, one per way a line reaches either parser. The
ideographic space is whitespace the runner's trim removes, as is the plain indent a gate prints.
"""

from __future__ import annotations

FORGING_NAMES = (
    "::error title=forged::all checks passed",
    "\u3000::warning::forged",
    "run-##[error]forged",
)


def opens_a_command(line: str) -> bool:
    """Whether the runner would hand `line` to a command parser at all."""
    return line.lstrip().startswith("::") or "##[" in line


def forged_lines(output: str) -> list[str]:
    """Every line of `output` the runner would read as a command.

    `str.splitlines` splits on more boundaries than the runner's line reader does, which only
    ever adds candidate lines, so a clean answer here is a clean answer there.
    """
    return [line for line in output.splitlines() if opens_a_command(line)]

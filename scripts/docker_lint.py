#!/usr/bin/env python3
"""Run the fleet Docker linters with bounded, observable execution."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PSSCRIPTANALYZER_VERSION = "1.23.0"
PSSCRIPTANALYZER_VOLUME = f"projecttemplate-psscriptanalyzer-{PSSCRIPTANALYZER_VERSION}"


@dataclass(frozen=True)
class Linter:
    """Describe one lint container and its tracked-file targets."""

    name: str
    image: str
    patterns: tuple[str, ...]
    workdir: str
    arguments: tuple[str, ...] = ()


LINTERS = (
    Linter("editorconfig-checker", "mstruebing/editorconfig-checker:latest", (), "/check"),
    Linter(
        "actionlint",
        "rhysd/actionlint:latest",
        (".github/workflows/*.yml", ".github/workflows/*.yaml"),
        "/repo",
        arguments=("-color",),
    ),
    Linter(
        "markdownlint",
        "davidanson/markdownlint-cli2:latest",
        ("*.md",),
        "/workdir",
    ),
    Linter(
        "cspell",
        "ghcr.io/streetsidesoftware/cspell:latest",
        ("README.md", "HISTORY.md"),
        "/workdir",
        arguments=("--no-progress",),
    ),
    Linter("shellcheck", "koalaman/shellcheck:stable", ("*.sh",), "/mnt"),
    Linter("PSScriptAnalyzer", "mcr.microsoft.com/powershell:latest", ("*.ps1",), "/mnt"),
)


class CommandFailed(RuntimeError):
    """Report a Docker command that failed or exceeded its bound."""


def run_command(
    command: Sequence[str], timeout: int, *, capture_output: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run one command and preserve its output for the caller."""
    try:
        return subprocess.run(
            command, check=False, text=True, timeout=timeout, capture_output=capture_output
        )
    except subprocess.TimeoutExpired as error:
        if command[:2] == ["docker", "run"] and "--name" in command:
            name = command[command.index("--name") + 1]
            try:
                cleanup = subprocess.run(
                    ["docker", "rm", "--force", name],
                    check=False,
                    timeout=min(timeout, 30),
                )
            except subprocess.TimeoutExpired as cleanup_error:
                raise CommandFailed(
                    f"timed out after {timeout}s; cleanup timed out for {name}"
                ) from cleanup_error
            if cleanup.returncode != 0:
                raise CommandFailed(
                    f"timed out after {timeout}s; cleanup failed for {name} "
                    f"(exit {cleanup.returncode})"
                ) from error
        raise CommandFailed(f"timed out after {timeout}s") from error


def tracked_files(root: Path, linter: Linter) -> list[str]:
    """Return the tracked files the linter can inspect."""
    command = [
        "git",
        "-C",
        str(root),
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    ]
    if linter.patterns:
        command.extend(["--", *linter.patterns])
    result = subprocess.run(command, check=True, capture_output=True)
    return [entry.decode() for entry in result.stdout.split(b"\0") if entry]


def docker_mount(root: Path, destination: str) -> str:
    """Build the read-only repository mount argument."""
    return f"type=bind,src={root},dst={destination},readonly"


def resolve_digest(
    image: str, timeout: int, runner: Callable[..., subprocess.CompletedProcess[str]]
) -> str:
    """Resolve the pulled image to its immutable repository digest."""
    result = runner(
        ["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", image],
        timeout,
        capture_output=True,
    )
    if result.returncode != 0:
        raise CommandFailed(f"container failure (exit {result.returncode})")
    digest = (result.stdout or "").strip()
    if not digest:
        raise CommandFailed("container failure (image has no repository digest)")
    return digest


def run_step(
    label: str,
    command: Sequence[str],
    timeout: int,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Run one visible bounded step and classify its result."""
    print(f"START {label} (timeout {timeout}s)", flush=True)
    try:
        result = runner(command, timeout)
    except CommandFailed as error:
        print(f"TIMEOUT {label}: {error}", flush=True)
        raise
    if result.returncode != 0:
        print(f"FAILED {label} (exit {result.returncode})", flush=True)
        raise CommandFailed(f"container failure (exit {result.returncode})")
    print(f"COMPLETE {label}", flush=True)


def powershell_command(files: Sequence[str]) -> str:
    """Build the analyzer command after targets are known."""
    quoted = ",".join("'" + file.replace("'", "''") + "'" for file in files)
    return (
        "Import-Module PSScriptAnalyzer; "
        f"$files = @({quoted}); "
        "$found = @(); "
        "foreach ($file in $files) { $found += Invoke-ScriptAnalyzer -Path $file "
        "-Settings ./PSScriptAnalyzerSettings.psd1 }; "
        "if ($found) { $found | Format-Table RuleName,Severity,ScriptName,Line,Message "
        "-AutoSize | Out-String -Width 200 | Write-Host; exit 1 }"
    )


def container_command(root: Path, linter: Linter, digest: str, files: Sequence[str]) -> list[str]:
    """Build a network-disabled, read-only lint invocation."""
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        f"projecttemplate-lint-{linter.name.lower()}-{os.getpid()}",
        "--network=none",
        "--mount",
        docker_mount(root, linter.workdir),
        "--workdir",
        linter.workdir,
    ]
    if linter.name == "PSScriptAnalyzer":
        command.extend(
            [
                "--mount",
                f"source={PSSCRIPTANALYZER_VOLUME},target=/root/.local/share/powershell/Modules,readonly",
                digest,
                "pwsh",
                "-NoProfile",
                "-Command",
                powershell_command(files),
            ]
        )
    else:
        command.extend([digest, *linter.arguments])
        if linter.name in {"markdownlint", "cspell", "shellcheck"}:
            command.extend(files)
    return command


def install_psscriptanalyzer(
    digest: str,
    timeout: int,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Populate the pinned analyzer module without exposing the repository."""
    run_step(
        "PSScriptAnalyzer module setup",
        ["docker", "volume", "create", PSSCRIPTANALYZER_VOLUME],
        timeout,
        runner,
    )
    run_step(
        "PSScriptAnalyzer module install",
        [
            "docker",
            "run",
            "--rm",
            "--name",
            f"projecttemplate-lint-psscriptanalyzer-setup-{os.getpid()}",
            "--mount",
            f"source={PSSCRIPTANALYZER_VOLUME},target=/root/.local/share/powershell/Modules",
            digest,
            "pwsh",
            "-NoProfile",
            "-Command",
            (
                "Set-PSRepository PSGallery -InstallationPolicy Trusted; "
                f"Install-Module PSScriptAnalyzer -RequiredVersion {PSSCRIPTANALYZER_VERSION} "
                "-Force -Scope CurrentUser"
            ),
        ],
        timeout,
        runner,
    )


def lint(
    root: Path,
    timeout: int,
    selected: set[str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = run_command,
) -> int:
    """Pull applicable images, then run every selected linter."""
    applicable: list[tuple[Linter, list[str]]] = []
    for linter in LINTERS:
        if linter.name not in selected:
            continue
        files = tracked_files(root, linter)
        if not files:
            print(f"SKIP {linter.name}: zero targets", flush=True)
            continue
        print(f"TARGETS {linter.name}: {len(files)} file(s)", flush=True)
        applicable.append((linter, files))

    if not applicable:
        print("COMPLETE lint: zero applicable linters", flush=True)
        return 0

    print("PHASE pull", flush=True)
    digests: dict[str, str] = {}
    try:
        for linter, _ in applicable:
            run_step(f"pull {linter.name}", ["docker", "pull", linter.image], timeout, runner)
            digests[linter.name] = resolve_digest(linter.image, timeout, runner)

        print("PHASE execution: pulls complete, repository mounts begin", flush=True)
        for linter, files in applicable:
            if linter.name == "PSScriptAnalyzer":
                install_psscriptanalyzer(digests[linter.name], timeout, runner)
            run_step(
                f"lint {linter.name} ({len(files)} file(s))",
                container_command(root, linter, digests[linter.name], files),
                timeout,
                runner,
            )
    except CommandFailed as error:
        print(f"RESULT failed: {error}", flush=True)
        return 1

    print(f"RESULT success: {len(applicable)} linter(s) completed", flush=True)
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    parser.add_argument(
        "--timeout", type=int, default=300, help="seconds allowed per Docker command"
    )
    parser.add_argument(
        "--linter",
        action="append",
        choices=[linter.name for linter in LINTERS],
        help="linter to run, repeat to select multiple (default: all)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line entry point."""
    args = parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")
    root = args.root.resolve()
    selected = set(args.linter or (linter.name for linter in LINTERS))
    return lint(root, args.timeout, selected)


if __name__ == "__main__":
    sys.exit(main())

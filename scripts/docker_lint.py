#!/usr/bin/env python3
"""Run the fleet Docker linters with bounded, observable execution."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PSSCRIPTANALYZER_VERSION = "1.23.0"
PSSCRIPTANALYZER_VOLUME = f"projecttemplate-psscriptanalyzer-{PSSCRIPTANALYZER_VERSION}"
MAX_FILE_ARGUMENT_BYTES = 16 * 1024


@dataclass(frozen=True)
class Linter:
    """Describe one lint container and its tracked-file targets."""

    name: str
    image: str
    patterns: tuple[str, ...]
    workdir: str
    arguments: tuple[str, ...] = ()
    # A `*.sh` pattern alone misses a tracked script meant to run as a bare command (no extension).
    # Shell linters set this so their target list also matches an extensionless shebang script.
    discover_shebang: bool = False


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
    Linter("shellcheck", "koalaman/shellcheck:stable", ("*.sh",), "/mnt", discover_shebang=True),
    Linter(
        "shfmt",
        "mvdan/shfmt:latest",
        ("*.sh",),
        "/mnt",
        arguments=("-d",),
        discover_shebang=True,
    ),
    Linter("PSScriptAnalyzer", "mcr.microsoft.com/powershell:latest", ("*.ps1",), "/mnt"),
)


class CommandFailed(RuntimeError):
    """Report a Docker command that failed or exceeded its bound."""


class CommandTimedOut(CommandFailed):
    """Report a Docker command that exceeded its bound."""


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
                raise CommandTimedOut(
                    f"timed out after {timeout}s; cleanup timed out for {name}"
                ) from cleanup_error
            except OSError as cleanup_error:
                raise CommandTimedOut(
                    f"timed out after {timeout}s; cleanup could not start for {name}: "
                    f"{cleanup_error}"
                ) from cleanup_error
            if cleanup.returncode != 0:
                raise CommandTimedOut(
                    f"timed out after {timeout}s; cleanup failed for {name} "
                    f"(exit {cleanup.returncode})"
                ) from error
        raise CommandTimedOut(f"timed out after {timeout}s") from error
    except OSError as error:
        raise CommandFailed(f"could not start command: {error}") from error


def ls_files(
    root: Path, patterns: Sequence[str] = (), *, include_untracked: bool = True
) -> list[str]:
    """Return tracked paths, plus unignored untracked ones unless told to skip those."""
    command = [
        "git",
        "-C",
        str(root),
        "ls-files",
        "-z",
        "--cached",
    ]
    if include_untracked:
        command.extend(["--others", "--exclude-standard"])
    if patterns:
        command.extend(["--", *patterns])
    try:
        result = subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as error:
        raise CommandFailed("target discovery failed: git was not found") from error
    except subprocess.CalledProcessError as error:
        detail = os.fsdecode(error.stderr).strip() if error.stderr else f"exit {error.returncode}"
        raise CommandFailed(f"target discovery failed: {detail}") from error
    except OSError as error:
        raise CommandFailed(f"target discovery failed: {error}") from error
    return [os.fsdecode(entry) for entry in result.stdout.split(b"\0") if entry]


# The env options below take a separate operand token, never mistaken for the command.
ENV_OPERAND_FLAGS = {"-u", "--unset", "-C", "--chdir"}


def _is_env_assignment(token: str) -> bool:
    """Report whether token is a `NAME=VALUE` env-style assignment."""
    name, separator, _ = token.partition("=")
    return (
        bool(separator)
        and bool(name)
        and (name[0].isalpha() or name[0] == "_")
        and all(char.isalnum() or char == "_" for char in name)
    )


def shell_shebang_interpreter(line: str) -> str | None:
    """Return the shebang's direct interpreter, bash or sh, or None otherwise.

    Tokenizes rather than substring-matches, so a plain-argument `bash` is not the interpreter.
    An `env` shebang walks past its own flags and `NAME=VALUE` assignments to find the command.
    """
    if not line.startswith("#!"):
        return None
    try:
        tokens = shlex.split(line[2:])
    except ValueError:
        return None
    if not tokens:
        return None
    interpreter = tokens[0].rsplit("/", 1)[-1]
    if interpreter in {"bash", "sh"}:
        return interpreter
    if interpreter != "env":
        return None
    args = tokens[1:]
    while args:
        token = args[0]
        if token == "--":
            args = args[1:]
            break
        if token in {"-S", "--split-string"}:
            args = args[1:]
            break
        if token.startswith("-"):
            args = args[2:] if token in ENV_OPERAND_FLAGS else args[1:]
            continue
        if _is_env_assignment(token):
            args = args[1:]
            continue
        break
    if args and args[0].rsplit("/", 1)[-1] in {"bash", "sh"}:
        return args[0].rsplit("/", 1)[-1]
    return None


def has_shell_shebang(root: Path, relative_path: str) -> bool:
    """Report whether a tracked file's shebang directly names bash or sh.

    Never follows a tracked symlink: `is_symlink()` uses `lstat`, keeping the target unreached.
    A read failure raises `CommandFailed` instead of returning `False`.
    That keeps a tracked file this cannot open from silently dropping out of the target list.
    """
    path = root / relative_path
    if path.is_symlink():
        return False
    try:
        with path.open("rb") as handle:
            first_line = handle.readline(256)
    except OSError as error:
        raise CommandFailed(
            f"target discovery failed: could not read {relative_path}: {error}"
        ) from error
    try:
        text = first_line.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError:
        return False
    return shell_shebang_interpreter(text) is not None


def extensionless_shell_scripts(root: Path) -> list[str]:
    """Return tracked, extension-less files whose shebang names bash or sh.

    A `*.sh` glob misses a script meant to run as a bare command, extension-less by design.
    Its shebang is the only signal `git ls-files` cannot glob for.
    Tracked only, matching CI's plain `git ls-files`.
    An untracked bare script would otherwise lint here but never in CI.
    """
    return [
        path
        for path in ls_files(root, include_untracked=False)
        if "." not in Path(path).name and has_shell_shebang(root, path)
    ]


def tracked_files(root: Path, linter: Linter) -> list[str]:
    """Return the tracked files the linter can inspect."""
    files = ls_files(root, linter.patterns)
    if linter.discover_shebang:
        files = sorted(set(files) | set(extensionless_shell_scripts(root)))
    return files


def docker_mount(root: Path, destination: str) -> str:
    """Build the read-only repository mount argument."""
    source = str(root).replace('"', '""')
    return f'type=bind,"src={source}",dst={destination},readonly'


def file_batches(linter: Linter, files: Sequence[str]) -> list[list[str]]:
    """Split file arguments before the host command-line limit becomes relevant."""
    if linter.name not in {"markdownlint", "cspell", "shellcheck", "shfmt", "PSScriptAnalyzer"}:
        return [list(files)]

    batches: list[list[str]] = []
    batch: list[str] = []
    batch_bytes = 0
    for file in files:
        multiplier = 2 if linter.name == "PSScriptAnalyzer" else 1
        file_bytes = len(os.fsencode(file)) * multiplier + 4
        if batch and batch_bytes + file_bytes > MAX_FILE_ARGUMENT_BYTES:
            batches.append(batch)
            batch = []
            batch_bytes = 0
        batch.append(file)
        batch_bytes += file_bytes
    if batch:
        batches.append(batch)
    return batches


def resolve_digest(
    name: str,
    image: str,
    timeout: int,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    """Resolve the pulled image to its immutable repository digest."""
    result = run_step(
        f"inspect {name}",
        ["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", image],
        timeout,
        runner,
        capture_output=True,
    )
    digest = (result.stdout or "").strip()
    if not digest:
        raise CommandFailed("container failure (image has no repository digest)")
    return digest


def run_step(
    label: str,
    command: Sequence[str],
    timeout: int,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one visible bounded step and classify its result."""
    print(f"START {label} (timeout {timeout}s)", flush=True)
    try:
        result = runner(command, timeout, capture_output=capture_output)
    except CommandTimedOut as error:
        print(f"TIMEOUT {label}: {error}", flush=True)
        raise
    except CommandFailed as error:
        print(f"FAILED {label}: {error}", flush=True)
        raise
    if result.returncode != 0:
        print(f"FAILED {label} (exit {result.returncode})", flush=True)
        raise CommandFailed(f"container failure (exit {result.returncode})")
    print(f"COMPLETE {label}", flush=True)
    return result


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
        if linter.name in {"markdownlint", "cspell", "shellcheck", "shfmt"}:
            command.append("--")
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
    try:
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
        for linter, _ in applicable:
            run_step(f"pull {linter.name}", ["docker", "pull", linter.image], timeout, runner)
            digests[linter.name] = resolve_digest(linter.name, linter.image, timeout, runner)

        print("PHASE execution: pulls complete, repository mounts begin", flush=True)
        for linter, files in applicable:
            if linter.name == "PSScriptAnalyzer":
                install_psscriptanalyzer(digests[linter.name], timeout, runner)
            batches = file_batches(linter, files)
            for index, batch in enumerate(batches, start=1):
                label = f"lint {linter.name} ({len(files)} file(s))"
                if len(batches) > 1:
                    label = (
                        f"lint {linter.name} batch {index}/{len(batches)} "
                        f"({len(batch)} of {len(files)} file(s))"
                    )
                run_step(
                    label,
                    container_command(root, linter, digests[linter.name], batch),
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

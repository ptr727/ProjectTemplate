#!/usr/bin/env python3
"""Run the fleet Docker linters with bounded, observable execution."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

PSSCRIPTANALYZER_VERSION = "1.23.0"
PSSCRIPTANALYZER_VOLUME = f"projecttemplate-psscriptanalyzer-{PSSCRIPTANALYZER_VERSION}"
MAX_FILE_ARGUMENT_BYTES = 16 * 1024
SNAPSHOT_PREFIX = "docker-lint-snapshot-"
OWNER_SUFFIX = ".owner"
GIT_TIMEOUT = 300
PIN_MANIFEST = ".github/actions/docker-lint-pins/Dockerfile"
PIN_LINE = re.compile(r"FROM (\S+:[^\s@/]+@sha256:[0-9a-f]{64}) AS ([a-z][a-z0-9-]*)")
PULL_ATTEMPTS = 3
PULL_BACKOFF = 15
GIT_LOCATION_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)


@dataclass(frozen=True)
class Linter:
    """Describe one lint container and its tracked-file targets."""

    name: str
    stage: str
    patterns: tuple[str, ...]
    workdir: str
    arguments: tuple[str, ...] = ()
    # A `*.sh` pattern alone misses a tracked script meant to run as a bare command (no extension).
    # Shell linters set this so their target list also matches an extensionless shebang script.
    discover_shebang: bool = False


# Under `glob` magic the leading `**/` also matches zero directories, which is what reaches the root copy as well as a nested one.
NODE_MODULES_EXCLUDE = ":(glob,exclude)**/node_modules/**"

LINTERS = (
    Linter(
        "editorconfig-checker",
        "editorconfig-checker",
        (),
        "/check",
    ),
    Linter(
        "actionlint",
        "actionlint",
        (".github/workflows/*.yml", ".github/workflows/*.yaml"),
        "/repo",
        arguments=("-color",),
    ),
    Linter(
        "markdownlint",
        "markdownlint",
        ("*.md",),
        "/workdir",
    ),
    Linter(
        "cspell",
        "cspell",
        ("README.md", "HISTORY.md"),
        "/workdir",
        arguments=("--no-progress",),
    ),
    Linter(
        "shellcheck",
        "shellcheck",
        ("*.sh",),
        "/mnt",
        discover_shebang=True,
    ),
    Linter(
        "shfmt",
        "shfmt",
        ("*.sh",),
        "/mnt",
        arguments=("-d",),
        discover_shebang=True,
    ),
    Linter("PSScriptAnalyzer", "powershell", ("*.ps1",), "/mnt"),
)


class CommandFailed(RuntimeError):
    """Report a Docker command that failed or exceeded its bound."""


class CommandTimedOut(CommandFailed):
    """Report a Docker command that exceeded its bound."""


class CommandExited(CommandFailed):
    """Report a Docker command that ran and exited non-zero."""


def read_pins(path: Path) -> dict[str, str]:
    """Return each image reference the pin manifest names, keyed by its stage.

    A line that pins no tag and digest is skipped, as the action and the repo gate skip it.

    A carrier's `hub_fetch_run.py` older than the manifest fetches this script alone.
    A missing manifest therefore names the resync rather than falling back to a moving tag.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise CommandFailed(
            f"the Docker lint pin manifest {PIN_MANIFEST} is not beside this script. "
            "Resync hub_fetch_run.py from ptr727/ProjectTemplate, "
            "since a current copy fetches the manifest with docker_lint.py"
        ) from error
    except (OSError, UnicodeDecodeError) as error:
        raise CommandFailed(f"could not read the pin manifest {path}: {error}") from error
    pins: dict[str, str] = {}
    for line in text.splitlines():
        match = PIN_LINE.fullmatch(line.rstrip())
        if match is None:
            continue
        if match.group(2) in pins:
            raise CommandFailed(f"the pin manifest {path} names the {match.group(2)} stage twice")
        pins[match.group(2)] = match.group(1)
    return pins


def container_name(command: Sequence[str]) -> str | None:
    """Return the name a `docker run` command gives its container, or None for any other command."""
    if command[:2] != ["docker", "run"] or "--name" not in command:
        return None
    return command[command.index("--name") + 1]


def remove_container(
    name: str, timeout: int, *, capture_output: bool = False
) -> subprocess.CompletedProcess[bytes]:
    """Force-remove a container within a bound shorter than its run's."""
    return subprocess.run(
        ["docker", "rm", "--force", name],
        check=False,
        capture_output=capture_output,
        timeout=min(timeout, 30),
    )


def run_command(
    command: Sequence[str], timeout: int, *, capture_output: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run one command and preserve its output for the caller.

    A timed-out or interrupted `docker run` has its container removed, since killing the client
    alone leaves the container running. An interrupt can reach this process and not the client.
    """
    try:
        return subprocess.run(
            command,
            check=False,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            capture_output=capture_output,
        )
    except subprocess.TimeoutExpired as error:
        name = container_name(command)
        if name is not None:
            try:
                cleanup = remove_container(name, timeout)
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
    except KeyboardInterrupt:
        name = container_name(command)
        if name is not None:
            try:
                cleanup = remove_container(name, timeout, capture_output=True)
            except (OSError, subprocess.SubprocessError) as cleanup_error:
                print(
                    f"interrupted; cleanup could not run for {name}: {cleanup_error}",
                    file=sys.stderr,
                )
            else:
                if cleanup.returncode != 0:
                    print(
                        f"interrupted; cleanup failed for {name} (exit {cleanup.returncode})",
                        file=sys.stderr,
                    )
        raise
    except OSError as error:
        raise CommandFailed(f"could not start command: {error}") from error


def git_environment() -> dict[str, str]:
    """Return this process's environment without the variables a git hook sets.

    One of them would name the hook's repository or index rather than the root this run lints.
    The list is `spec/git_env.py`'s, copied so the fetched wrapper runs with no hub file beside it.
    """
    return {k: v for k, v in os.environ.items() if k not in GIT_LOCATION_VARS}


def ls_files(
    root: Path, patterns: Sequence[str] = (), *, include_untracked: bool = True
) -> list[str]:
    """Return tracked paths, plus unignored untracked ones unless told to skip those.

    A tracked path deleted from disk is dropped, since a linter given it fails on a missing file.
    A local `npm install` is untracked and unignored in a repository whose `.gitignore` omits it,
    so its third-party files would otherwise be lint targets.
    Dropping them here rather than in a linter config is what reaches shellcheck and shfmt, which
    have no ignore config of their own to carry.
    """
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
    command.extend(["--", *patterns, NODE_MODULES_EXCLUDE])
    try:
        result = subprocess.run(command, env=git_environment(), check=True, capture_output=True)
    except FileNotFoundError as error:
        raise CommandFailed("target discovery failed: git was not found") from error
    except subprocess.CalledProcessError as error:
        detail = os.fsdecode(error.stderr).strip() if error.stderr else f"exit {error.returncode}"
        raise CommandFailed(f"target discovery failed: {detail}") from error
    except OSError as error:
        raise CommandFailed(f"target discovery failed: {error}") from error
    paths = [os.fsdecode(entry) for entry in result.stdout.split(b"\0") if entry]
    return [path for path in paths if os.path.lexists(root / path)]


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


def snapshot_parent(root: Path) -> Path:
    """Return the checkout's own git directory, where the snapshot is staged.

    Any Docker setup that can mount the checkout reaches it, and git never lists it.
    The system temporary directory is outside Docker Desktop for Linux's default file sharing.
    `--absolute-git-dir` resolves per worktree, so two worktrees of one repository never share it.
    """
    command = ["git", "rev-parse", "--absolute-git-dir"]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=git_environment(),
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CommandFailed(
            f"snapshot failed: {shlex.join(command)} could not be run ({error})"
        ) from error
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip()
        raise CommandFailed(f"snapshot failed: {shlex.join(command)} failed: {detail}")
    return Path(os.fsdecode(result.stdout).rstrip("\n"))


received: list[int] = []


def on_signal(signum: int, _frame: object) -> None:
    """Record the signal and raise KeyboardInterrupt, so every signal unwinds one path.

    The unwinding removes the snapshot. A later signal is passed over, since it would cut the
    unwinding short. One is the SIGTERM that `uv run` forwards to a process group the terminal's
    SIGINT already reached.
    """
    if received:
        return
    received.append(signum)
    raise KeyboardInterrupt


def lock_owner(handle: IO[str], *, wait: bool) -> bool:
    """Lock an owner file's first byte, the lock its run holds until the snapshot is gone.

    The OS drops the lock when the holding process dies, however it was ended.
    """
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except OSError:
        if wait:
            raise
        return False
    return True


def unlock_owner(handle: IO[str]) -> None:
    """Release the lock before closing, since Windows leaves a lock closed with its file to the OS."""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def remove_snapshot(snapshot: Path) -> bool:
    """Remove a snapshot, whose top directory takes the checkout's mode and so may be read-only."""
    with contextlib.suppress(OSError):
        snapshot.chmod(stat.S_IRWXU)
    shutil.rmtree(snapshot, onexc=unlink_read_only)
    return not os.path.lexists(snapshot)


def unlink_read_only(func: object, path: str, exc: BaseException) -> None:
    """An rmtree error handler that removes a read-only file, and swallows anything else.

    POSIX unlinks a read-only file, since that needs write access to its directory, but Windows refuses.
    Any other failure is left for `remove_snapshot`'s own existence check to report.
    """
    if func is not os.unlink or not isinstance(exc, PermissionError):
        return
    try:
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)
    except OSError:
        pass


def remove_stale_snapshots(parent: Path) -> int:
    """Remove each snapshot whose run ended without removing it, a killed task's for one.

    A snapshot is stale only where its owner file names a run that wrote it and holds no lock.
    A run writes that file only once it holds the lock, so an empty one is a run still starting.
    A directory with no owner file is never touched.
    """
    removed = 0
    for owner in sorted(parent.glob(f"{SNAPSHOT_PREFIX}*{OWNER_SUFFIX}")):
        try:
            with owner.open("r+", encoding="utf-8") as handle:
                if not lock_owner(handle, wait=False):
                    continue
                stale = bool(handle.read())
                unlock_owner(handle)
        except OSError:
            continue
        if stale:
            snapshot = owner.with_name(owner.name.removesuffix(OWNER_SUFFIX))
            # An owner file outlives a snapshot that could not be removed, so a later run retries it.
            if remove_snapshot(snapshot):
                owner.unlink(missing_ok=True)
                removed += 1
    return removed


@contextlib.contextmanager
def owned_snapshot(parent: Path) -> Iterator[Path]:
    """Create an empty snapshot directory, held by a locked owner file beside it until removed.

    The owner file is unlinked once its handle is closed, since Windows cannot unlink an open file.
    That unlink is in a finally clause, so an interrupt's unwinding removes it too.
    """
    descriptor, name = tempfile.mkstemp(prefix=SNAPSHOT_PREFIX, suffix=OWNER_SUFFIX, dir=parent)
    owner = Path(name)
    removed = False
    try:
        with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
            lock_owner(handle, wait=True)
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            snapshot = owner.with_name(owner.name.removesuffix(OWNER_SUFFIX))
            snapshot.mkdir()
            try:
                yield snapshot
            finally:
                removed = remove_snapshot(snapshot)
                unlock_owner(handle)
    finally:
        if removed:
            owner.unlink(missing_ok=True)


def stage_snapshot(root: Path, snapshot: Path) -> int:
    """Copy the tracked and unignored files into snapshot, the only tree a container mounts.

    A git-ignored file, such as a local secrets file beside a tracked placeholder, stays behind.
    A symlink is copied as a link and never followed.
    A tracked path missing from disk never reaches it, since `ls_files` drops it.
    An empty `.git` is added, since actionlint finds its project only where one sits beside
    `.github/workflows`.
    """
    staged = 0
    for relative_path in ls_files(root):
        source = root / relative_path
        destination = snapshot / relative_path
        try:
            if not source.is_symlink() and source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination, follow_symlinks=False)
        except OSError as error:
            raise CommandFailed(
                f"snapshot failed: could not copy {relative_path}: {error}"
            ) from error
        staged += 1
    (snapshot / ".git").mkdir(exist_ok=True)
    snapshot.chmod(stat.S_IMODE(root.stat().st_mode))
    return staged


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
        detail = (result.stderr or "").strip() if capture_output else ""
        suffix = f": {detail}" if detail else ""
        raise CommandExited(f"container failure (exit {result.returncode}){suffix}")
    print(f"COMPLETE {label}", flush=True)
    return result


def pull_image(
    name: str,
    image: str,
    timeout: int,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Pull one pinned image, retrying a pull that exits non-zero a bounded number of times.

    A transient registry error fails a pull that a later attempt completes.
    A timeout or a client that cannot start is not retried, since a retry would meet it again.
    The client's error output is captured into the failure, so a caller reporting it names the cause.
    This is the one pull retry: the repo gate and validate-task.yml's lint steps pull through it too.
    The waits are 15s, then 30s, the widest of the schedules it replaced. A connection reset on
    Docker Hub's token endpoint once failed a release, and that blip clears within such a window.
    """
    for attempt in range(1, PULL_ATTEMPTS + 1):
        label = f"pull {name}" if attempt == 1 else f"pull {name} attempt {attempt}/{PULL_ATTEMPTS}"
        try:
            run_step(
                label, ["docker", "pull", "--quiet", image], timeout, runner, capture_output=True
            )
        except CommandExited:
            if attempt == PULL_ATTEMPTS:
                annotate("error", f"docker pull of {image} failed on all {PULL_ATTEMPTS} attempts.")
                raise
            annotate("warning", f"docker pull of {image} failed on attempt {attempt}.")
            delay = PULL_BACKOFF * attempt
            print(f"RETRY pull {name} in {delay}s", flush=True)
            time.sleep(delay)
        except CommandFailed:
            annotate("error", f"docker pull of {image} timed out or could not start.")
            raise
        else:
            return


def annotate(level: str, message: str) -> None:
    """Under GitHub Actions, show `message` on the run's summary, which a plain log line never reaches.

    The message is built from the image reference alone, never from Docker's own output.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}", flush=True)


def pull(
    images: Sequence[str],
    timeout: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] = run_command,
) -> int:
    """Pull each image with the bounded retry and lint nothing.

    A caller that runs its own container pulls through here, so every pull shares one schedule.
    """
    try:
        for image in images:
            pull_image(image, image, timeout, runner)
    except CommandFailed as error:
        print(f"RESULT failed: {error}", flush=True)
        return 1
    print(f"RESULT success: {len(images)} image(s) pulled", flush=True)
    return 0


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
    pins: Mapping[str, str] | None = None,
) -> int:
    """Pull applicable images, then run every selected linter.

    The pins default to the manifest at its hub-relative path from this script.
    """
    try:
        if pins is None:
            pins = read_pins(Path(__file__).resolve().parents[1] / PIN_MANIFEST)
        missing = sorted({linter.stage for linter in LINTERS} - set(pins))
        if missing:
            raise CommandFailed(f"the pin manifest names no {', '.join(missing)} stage")
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

        parent = snapshot_parent(root)
        if swept := remove_stale_snapshots(parent):
            print(f"SWEPT {swept} snapshot(s) an ended run left behind", flush=True)
        if not applicable:
            print("COMPLETE lint: zero applicable linters", flush=True)
            return 0

        print("PHASE pull", flush=True)
        digests: dict[str, str] = {}
        for linter, _ in applicable:
            image = pins[linter.stage]
            pull_image(linter.name, image, timeout, runner)
            digests[linter.name] = resolve_digest(linter.name, image, timeout, runner)

        print("PHASE execution: pulls complete, repository mounts begin", flush=True)
        with owned_snapshot(parent) as snapshot:
            staged = stage_snapshot(root, snapshot)
            print(f"SNAPSHOT {staged} tracked or unignored path(s) staged to mount", flush=True)
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
                        container_command(snapshot, linter, digests[linter.name], batch),
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--linter",
        action="append",
        choices=[linter.name for linter in LINTERS],
        help="linter to run, repeat to select multiple (default: all)",
    )
    mode.add_argument(
        "--pull",
        action="append",
        metavar="IMAGE",
        help="only pull IMAGE with the bounded retry, repeat to pull several, and lint nothing",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int | signal.Signals:
    """Run the command-line entry point, returning its status, or the signal that interrupted it.

    A SIGINT inherited as ignored, as a background child's is, stays ignored.
    """
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, on_signal)
    if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
        signal.signal(signal.SIGINT, on_signal)
    # Closing a VS Code task's terminal sends SIGHUP rather than SIGTERM.
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, on_signal)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")
    root = args.root.resolve()
    selected = set(args.linter or (linter.name for linter in LINTERS))
    try:
        if args.pull:
            return pull(args.pull, args.timeout)
        return lint(root, args.timeout, selected)
    except KeyboardInterrupt:
        signum = signal.Signals(received[0] if received else signal.SIGINT)
        print(f"docker_lint.py: interrupted by {signum.name}.", file=sys.stderr)
        return signum


def reraise(signum: signal.Signals) -> None:
    """End this process by `signum`, so a caller sees the signal rather than an exit status."""
    with contextlib.suppress(OSError):
        sys.stdout.flush()
        sys.stderr.flush()
    if sys.platform == "win32":
        sys.exit(130)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    sys.exit(128 + signum)


if __name__ == "__main__":
    status = main()
    if isinstance(status, signal.Signals):
        reraise(status)
    sys.exit(status)

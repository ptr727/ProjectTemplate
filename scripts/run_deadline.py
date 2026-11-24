"""A captured child run whose deadline holds on Windows, for the scripts beside this one.

This is a library module and has no command line. `subprocess.run` on Windows kills only the direct
child at its deadline, then waits with no bound for the pipes to close. A child of a launcher holds
them open, so that wait lasts as long as the stall. Git for Windows' `cmd\\git.exe` and an npm
`.cmd` shim are such launchers. Here the tree is killed and the final collection is bounded.

Output decodes here, in the caller's thread, because text mode decodes on a reader thread on
Windows. A decode failure there prints a traceback and never reaches the caller.
"""

from __future__ import annotations

import contextlib
import locale
import os
import subprocess

KILL_GRACE_SECONDS = 5


def kill_process_tree(process: subprocess.Popen) -> None:
    """Kill `process` and, on Windows, what it started, since a launcher's child outlives it alone."""
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=KILL_GRACE_SECONDS,
            )
    process.kill()


def run_with_deadline(
    argv: list[str],
    timeout: float,
    *,
    text: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    **popen,
) -> subprocess.CompletedProcess:
    """Run `argv` captured, raising `TimeoutExpired` close to `timeout` seconds.

    `text`, `encoding`, and `errors` mean what they mean to `subprocess.run`, newline translation
    included, and with none of them the output stays bytes. Stderr is captured unless `popen` names
    another `stderr`. Every other keyword goes to `subprocess.Popen` as it is. An interrupt kills
    the child as well, as it does under `subprocess.run`.
    """
    popen.setdefault("stderr", subprocess.PIPE)
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, **popen)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(process)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=KILL_GRACE_SECONDS)
        raise
    except BaseException:
        kill_process_tree(process)
        raise
    if text or encoding is not None or errors is not None:
        stdout, stderr = (_decoded(s, encoding, errors) for s in (stdout, stderr))
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _decoded(data: bytes | None, encoding: str | None, errors: str | None) -> str | None:
    """`data` as text mode would read it, None where the stream was not captured."""
    if data is None:
        return None
    decoded = data.decode(encoding or locale.getpreferredencoding(False), errors or "strict")
    return decoded.replace("\r\n", "\n").replace("\r", "\n")

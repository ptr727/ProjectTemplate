#!/usr/bin/env python3
"""Sync secret files between a home directory and 1Password through the `op` CLI.

A bucket is a named set of secret files owned by one project or host. Each bucket is one vault
named exactly `Secrets <bucket>`, and each file is one Document item in it. An item's title is the
file's POSIX path relative to the home directory, and its mode rides as a `mode:<octal>` tag.

Nothing is tracked locally. The live comparison between the vault and the home directory is the
only truth, so no state file can drift from either side. Consumers keep reading the flat files, and
nothing but this tool reads `op`.

Every `op` call goes through `run_op`, one at a time. The session reaches a child through its
environment and never through its argument list. Output never carries a secret, a full digest, or
any of `op`'s JSON. A digest is shown as its first eight hex characters at most.

Several `op` behaviors are assumptions no official page states. They are the byte-exact document
round-trip, the item list's `updated_at` and `tags` fields, and `--tags` replacing rather than
adding. Each is read strictly here, so a different shape fails loudly rather than being guessed at.
The `selftest` subcommand measures each one against a scratch vault.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import hashlib
import json
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
import typing
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import FrameType

VAULT_PREFIX = "Secrets "
BUCKET = re.compile(r"[a-z0-9-]+")
MODE_TAG = re.compile(r"mode:([0-7]{3,4})")
MODE_ARG = re.compile(r"[0-7]{3,4}")
SESSION_LINE = re.compile(
    r"""\s*(?:export\s+|\$env:)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(["'])([^"'\r\n]+)\2\s*;?\s*"""
)
RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{n}" for n in range(1, 10)),
        *(f"LPT{n}" for n in range(1, 10)),
    }
)
WINDOWS_FORBIDDEN = frozenset('\\:<>"|?*')
SECRETS_DIR = ".secrets"
TEMP_PREFIX = ".secrets-sync-"
HASH_SHOWN = 8
STDERR_CAP = 2000
IS_WINDOWS = sys.platform == "win32"
WRITERS = frozenset({"push", "pull", "archive", "selftest"})


class Parser(argparse.ArgumentParser):
    """An `ArgumentParser` whose usage errors exit 1, so 2 means only that the command did not run."""

    def error(self, message: str) -> typing.NoReturn:
        self.print_usage(sys.stderr)
        sys.stderr.write(f"refused: {self.prog}: {message}\n")
        raise SystemExit(1)


class Refusal(Exception):
    """A state the caller can act on, reported at exit 1."""


class Execution(Exception):
    """The command did not run to an answer, reported at exit 2."""


class Outcome(typing.NamedTuple):
    returncode: int
    stdout: bytes
    stderr: bytes


def run_op(argv: Sequence[str], env: Mapping[str, str], attached: bool) -> Outcome:
    """Run one `op` command and return its exit status and both streams as bytes.

    Every call reaches `op` here, so the tests replace this one function with a fake.
    Output stays bytes, since a document body is a secret that must round-trip byte-exact.
    An attached call inherits the terminal's stdin and stderr, which is how `op signin` prompts.
    The child is stopped in `finally`, so an interrupt never leaves it running.
    """
    try:
        child = subprocess.Popen(
            ["op", *argv],
            stdin=None if attached else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=None if attached else subprocess.PIPE,
            env=dict(env),
        )
    except OSError as exc:
        raise Execution(f"could not run op: {exc.strerror or type(exc).__name__}") from exc
    try:
        out, err = child.communicate()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait()
    return Outcome(child.returncode, out, err or b"")


def report_stderr(err: bytes) -> None:
    """Print a failed call's stderr, capped, since an unbounded one buries the line that matters."""
    detail = err.decode("utf-8", errors="replace").strip()
    if not detail:
        return
    sys.stderr.write(detail[:STDERR_CAP])
    if len(detail) > STDERR_CAP:
        sys.stderr.write(f" ... {len(detail) - STDERR_CAP} more character(s) not shown")
    sys.stderr.write("\n")


class Op:
    """The `op` calls one run makes, with its session and its dry-run switch.

    A dry run prints each write and sends none of it, on the same path every read takes.
    """

    def __init__(self, account: str | None, dry_run: bool) -> None:
        self.account = account
        self.dry_run = dry_run
        self.session: dict[str, str] = {}

    def env(self) -> dict[str, str]:
        return {**os.environ, **self.session}

    def argv(self, args: Sequence[str]) -> list[str]:
        """`args` scoped to the named account, except the listing that counts the accounts."""
        if not self.account or list(args[:2]) == ["account", "list"]:
            return list(args)
        return [*args, "--account", self.account]

    def probe(self, args: Sequence[str]) -> Outcome:
        """One read whose failure is an answer rather than an error, such as `whoami`."""
        return run_op(self.argv(args), self.env(), attached=False)

    def call(self, args: Sequence[str], *, write: bool = False, detail: str = "") -> bytes:
        argv = self.argv(args)
        if write and self.dry_run:
            print(f"  would run: op {shlex.join(argv)}")
            return b""
        sys.stderr.write(f"op {' '.join(args[:2])}{f' {detail}' if detail else ''}\n")
        sys.stderr.flush()
        result = run_op(argv, self.env(), attached=False)
        if result.returncode != 0:
            report_stderr(result.stderr)
            raise Execution(f"op {' '.join(args[:2])} failed rc={result.returncode}")
        return result.stdout

    def json(self, args: Sequence[str], detail: str = "") -> object:
        out = self.call(args, detail=detail)
        try:
            return json.loads(out.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Execution(f"op {' '.join(args[:2])} returned output that is not JSON") from exc


def short(data: bytes | None) -> str:
    return "-" if data is None else hashlib.sha256(data).hexdigest()[:HASH_SHOWN]


def stamp(moment: datetime.datetime | None) -> str:
    return "-" if moment is None else moment.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def home_dir() -> Path:
    return Path(os.path.abspath(Path.home()))


def ask(question: str) -> bool:
    sys.stdout.write(f"{question} [y/N] ")
    sys.stdout.flush()
    return sys.stdin.readline().strip().lower() in {"y", "yes"}


def refuse_root() -> None:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        raise Refusal(
            "this tool does not run as root, since it would write root-owned secret files"
        )


def parse_session(out: bytes) -> tuple[str, str] | None:
    """The one variable assignment `op signin` prints, in its POSIX or its PowerShell form.

    The variable's name is read rather than assumed, since `op` derives it from the account.
    None means no assignment, the output of a sign-in the desktop app holds instead.
    """
    try:
        text = out.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Execution("op signin printed output that is not UTF-8") from exc
    found = [m for line in text.splitlines() if (m := SESSION_LINE.fullmatch(line))]
    if not found:
        return None
    if len(found) > 1:
        raise Execution(
            f"op signin printed {len(found)} session assignment(s) where one was expected"
        )
    return found[0].group(1), found[0].group(3)


def count_accounts(data: object) -> int:
    """The number of configured accounts, from a JSON array whose row shape is an assumption."""
    if not isinstance(data, list):
        raise Execution("op account list returned JSON that is not an array")
    return len(data)


@contextlib.contextmanager
def session(op: Op) -> Iterator[None]:
    """Use the ambient session where `op whoami` accepts it, and otherwise sign in for this run.

    A session this run created is signed out in `finally`, which also runs on SIGINT and SIGTERM.
    An ambient session belongs to the caller and is left alone.
    """
    accounts = count_accounts(op.json(["account", "list", "--format", "json"]))
    if accounts == 0:
        raise Refusal("no 1Password account is configured, so add one with 'op account add' first")
    if accounts > 1 and not op.account:
        raise Refusal(f"{accounts} 1Password accounts are configured, so name one with --account")
    if op.probe(["whoami"]).returncode == 0:
        yield
        return
    if not stdin_is_tty():
        raise Refusal("no 1Password session is active, and signing in needs a terminal on stdin")
    sys.stderr.write("op signin\n")
    signin = run_op(op.argv(["signin"]), op.env(), attached=True)
    if signin.returncode != 0:
        raise Execution(f"op signin failed rc={signin.returncode}")
    parsed = parse_session(signin.stdout)
    if parsed is None:
        if op.probe(["whoami"]).returncode != 0:
            raise Execution("op signin printed no session assignment, and op whoami still refuses")
        yield
        return
    op.session = {parsed[0]: parsed[1]}
    try:
        if op.probe(["whoami"]).returncode != 0:
            raise Execution("op whoami does not accept the session op signin printed")
        yield
    finally:
        signout(op)


def signout(op: Op) -> None:
    """Sign out a session this run created, reporting a failure rather than hiding it."""
    result = run_op(op.argv(["signout"]), op.env(), attached=False)
    if result.returncode != 0:
        report_stderr(result.stderr)
        sys.stderr.write(
            f"warning: op signout failed rc={result.returncode}, so the session stays valid until it idles out\n"
        )


def recheck(op: Op) -> None:
    """Confirm the session outlived the prompt, since `op` ends one after 30 idle minutes."""
    if op.probe(["whoami"]).returncode != 0:
        raise Refusal("the 1Password session ended during the prompt, so nothing was written")


def confirm(op: Op, yes: bool, question: str, *, then_op: bool = True) -> None:
    if yes:
        return
    if not ask(question):
        raise Refusal("not confirmed, so nothing was written")
    if then_op:
        recheck(op)


def vault_name(bucket: str) -> str:
    return f"{VAULT_PREFIX}{bucket}"


def validate_title(title: str) -> str:
    """Refuse a title that is not a safe relative path on every platform alike.

    A title becomes a path under the home directory, so a title one platform reads differently
    from another can land a secret somewhere nobody chose.
    """
    if not title:
        raise Refusal("an item title is empty")
    if any(ord(c) < 32 or ord(c) == 127 for c in title):
        raise Refusal(f"item title {title!r} carries a control character")
    if title.startswith("/"):
        raise Refusal(f"item title {title!r} is an absolute path")
    if any(c in WINDOWS_FORBIDDEN for c in title):
        raise Refusal(f"item title {title!r} carries a character Windows forbids in a name")
    for part in title.split("/"):
        if part in {"", ".", ".."}:
            raise Refusal(f"item title {title!r} has an empty, '.', or '..' component")
        if part.endswith((".", " ")):
            raise Refusal(f"item title {title!r} has a component ending in a dot or a space")
        if part.split(".")[0].upper() in RESERVED:
            raise Refusal(f"item title {title!r} has a component Windows reserves")
    return title


def local_path(home: Path, title: str) -> Path:
    return home.joinpath(*title.split("/"))


def title_for(home: Path, raw: str) -> str:
    """The title for a path under the home directory, spelled as the directory spells it.

    A case-insensitive file system finds a file under a miscased path, so the title would carry
    that case. A case-sensitive host then writes it where no consumer reads.
    """
    absolute = Path(os.path.abspath(raw))
    try:
        relative = absolute.relative_to(home)
    except ValueError as exc:
        raise Refusal(f"{raw} is not under the home directory {home}") from exc
    current = home
    for part in relative.parts:
        if current.is_dir():
            names = os.listdir(current)
            if part not in names and any(n.casefold() == part.casefold() for n in names):
                raise Refusal(f"{raw} differs in case from the name under {current}")
        current = current / part
    return validate_title(relative.as_posix())


def is_link(path: Path) -> bool:
    """A symbolic link, or on Windows a directory junction, which `is_symlink` reads as False.

    Python 3.12 raises where a parent denies search, so that case is refused by name.
    """
    try:
        return path.is_symlink() or path.is_junction()
    except OSError as exc:
        raise Refusal(f"{path} cannot be inspected: {exc.strerror or type(exc).__name__}") from exc


def first_link(home: Path, title: str) -> Path | None:
    """The first link on a title's path below the home directory, or None where it has none."""
    current = home
    for part in title.split("/"):
        current = current / part
        if is_link(current):
            return current
    return None


def refuse_symlinks(home: Path, title: str) -> None:
    """Refuse a title whose path below the home directory passes through a link."""
    link = first_link(home, title)
    if link is not None:
        raise Refusal(
            f"{title}: {link} is a symbolic link, which this tool never reads or writes through"
        )


@dataclasses.dataclass(frozen=True)
class Vault:
    id: str
    name: str


@dataclasses.dataclass(frozen=True)
class Item:
    id: str
    title: str
    tags: tuple[str, ...]
    updated: datetime.datetime


@dataclasses.dataclass(frozen=True)
class Local:
    data: bytes | None
    mode: int | None
    mtime: datetime.datetime | None


def rows(data: object, what: str, fields: Sequence[str]) -> list[dict[str, object]]:
    """`data` as a list of objects each carrying a string in every one of `fields`."""
    if not isinstance(data, list):
        raise Execution(f"{what} returned JSON that is not an array")
    out: list[dict[str, object]] = []
    for row in data:
        if not isinstance(row, dict) or not all(isinstance(row.get(f), str) for f in fields):
            raise Execution(f"{what} returned a row without string fields {', '.join(fields)}")
        out.append(row)
    return out


def list_vaults(op: Op) -> list[Vault]:
    data = op.json(["vault", "list", "--format", "json"])
    return [
        Vault(str(r["id"]), str(r["name"])) for r in rows(data, "op vault list", ("id", "name"))
    ]


def find_vault(vaults: Sequence[Vault], bucket: str) -> Vault | None:
    """The vault named exactly for `bucket`, refusing where more than one carries the name."""
    name = vault_name(bucket)
    hits = [v for v in vaults if v.name == name]
    if len(hits) > 1:
        raise Refusal(
            f"{len(hits)} vaults are named {name!r}, so which one holds the bucket is ambiguous"
        )
    return hits[0] if hits else None


def parse_item(row: dict[str, object]) -> Item:
    """One item list row, its `tags` and `updated_at` field names being assumptions `selftest` measures.

    An absent `tags` field reads as no tags, the shape a listing gives an untagged item.
    """
    tags = row.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise Execution("op item list returned a 'tags' field that is not a list of strings")
    updated = row.get("updated_at")
    if not isinstance(updated, str):
        raise Execution("op item list returned a row with no 'updated_at' string, so run selftest")
    try:
        moment = datetime.datetime.fromisoformat(updated)
    except ValueError as exc:
        raise Execution("op item list returned an 'updated_at' that is not ISO 8601") from exc
    if moment.tzinfo is None:
        raise Execution("op item list returned an 'updated_at' with no time zone")
    return Item(str(row["id"]), str(row["title"]), tuple(tags), moment)


def members(op: Op, vault: Vault) -> dict[str, Item]:
    """The vault's Document items by title, refusing an unsafe, duplicated, or case-colliding title."""
    data = op.json(
        ["item", "list", "--vault", vault.id, "--categories", "Document", "--format", "json"],
        detail=vault.name,
    )
    items: dict[str, Item] = {}
    folded: dict[str, str] = {}
    for row in rows(data, "op item list", ("id", "title")):
        item = parse_item(row)
        validate_title(item.title)
        if item.title in items:
            raise Refusal(f"{vault.name} holds more than one item titled {item.title!r}")
        other = folded.setdefault(item.title.casefold(), item.title)
        if other != item.title:
            raise Refusal(
                f"{vault.name} holds {other!r} and {item.title!r}, which collide case-insensitively"
            )
        items[item.title] = item
    return items


def mode_of(item: Item) -> int:
    """The mode an item's one `mode:<octal>` tag carries, refusing a missing or malformed tag."""
    tags = [t for t in item.tags if t.startswith("mode:")]
    if len(tags) != 1:
        raise Refusal(f"{item.title} carries {len(tags)} mode tag(s) where exactly one is required")
    match = MODE_TAG.fullmatch(tags[0])
    value = int(match.group(1), 8) if match else -1
    if not 0 <= value <= 0o777:
        raise Refusal(f"{item.title} carries the malformed mode tag {tags[0]!r}")
    return value


def mode_or_none(item: Item) -> int | None:
    try:
        return mode_of(item)
    except Refusal:
        return None


def mode_tag(mode: int) -> str:
    return f"mode:{mode:04o}"


def tags_with_mode(item: Item | None, mode: int) -> str:
    """The item's other tags plus the one mode tag, as the full list `--tags` replaces."""
    kept = [t for t in (item.tags if item else ()) if not t.startswith("mode:")]
    return ",".join([*kept, mode_tag(mode)])


def download(op: Op, vault: Vault, item: Item) -> bytes:
    return op.call(["document", "get", item.id, "--vault", vault.id], detail=item.title)


def read_local(path: Path) -> Local:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise Refusal(f"{path} is not a regular file")
        data = path.read_bytes()
    except FileNotFoundError:
        return Local(None, None, None)
    except OSError as exc:
        raise Refusal(f"{path} cannot be read: {exc.strerror or type(exc).__name__}") from exc
    moment = datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.UTC)
    return Local(data, stat.S_IMODE(st.st_mode), moment)


def selected(
    op: Op, vaults: Sequence[Vault], buckets: Sequence[str]
) -> list[tuple[Vault, dict[str, Item]]]:
    """Each named bucket's vault and items, refusing an absent vault or a title two buckets share."""
    chosen: list[tuple[Vault, dict[str, Item]]] = []
    owner: dict[str, str] = {}
    for bucket in dict.fromkeys(buckets):
        vault = find_vault(vaults, bucket)
        if vault is None:
            raise Refusal(f"no vault is named {vault_name(bucket)!r}")
        items = members(op, vault)
        for title in items:
            held = owner.setdefault(title.casefold(), f"{vault.name}:{title}")
            if held != f"{vault.name}:{title}":
                raise Refusal(f"{title} is held by both {held.split(':')[0]} and {vault.name}")
        chosen.append((vault, items))
    return chosen


def secrets_files(home: Path) -> list[str]:
    """Titles of the regular files under the home's `.secrets` directory, read by name only.

    The list is informational, so an entry that cannot be read is left out rather than failing.
    """
    root = home / SECRETS_DIR
    try:
        if is_link(root) or not root.is_dir():
            return []
    except Refusal:
        return []
    found: list[str] = []
    for directory, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            with contextlib.suppress(OSError):
                if stat.S_ISREG(os.lstat(path).st_mode):
                    found.append(path.relative_to(home).as_posix())
    return sorted(found)


def compare(local: Local, remote: bytes, mode: int | None) -> str:
    if local.data is None:
        return "only-remote"
    if local.data != remote:
        return "differs"
    if mode is not None and local.mode != mode:
        return "mode-differs"
    return "same"


def inspect(
    op: Op, home: Path, vault: Vault, title: str, item: Item
) -> tuple[Local, bytes, int | None, str]:
    """One item's local and remote copies and their state, as `status` and `pull` both judge it."""
    mode = None if IS_WINDOWS else mode_of(item)
    refuse_symlinks(home, title)
    local = read_local(local_path(home, title))
    remote = download(op, vault, item)
    return local, remote, mode, compare(local, remote, mode)


def cmd_status(a: argparse.Namespace, op: Op) -> int:
    home = home_dir()
    vaults = list_vaults(op)
    if a.bucket:
        for vault, items in selected(op, vaults, a.bucket):
            print(f"{vault.name}:")
            for title, item in sorted(items.items()):
                local, remote, _, state = inspect(op, home, vault, title, item)
                print(f"  {state:<12} {title}  remote {short(remote)}  local {short(local.data)}")
        return 0
    held: set[str] = set()
    for name in sorted({v.name for v in vaults if v.name.startswith(VAULT_PREFIX)}):
        bucket = name[len(VAULT_PREFIX) :]
        found = find_vault(vaults, bucket) if BUCKET.fullmatch(bucket) else None
        if found is None:
            print(f"{name}: not a bucket name, skipped")
            continue
        items = members(op, found)
        print(f"{found.name}:")
        for title in sorted(items):
            if first_link(home, title) is not None:
                there = "link"
            else:
                there = "present" if os.path.lexists(local_path(home, title)) else "absent"
            print(f"  {there:<8} {title}")
            held.add(title)
    unheld = [t for t in secrets_files(home) if t not in held]
    if unheld:
        print(f"Files under ~/{SECRETS_DIR} that no bucket holds (informational):")
        for title in unheld:
            print(f"  {title}")
    return 0


@dataclasses.dataclass(frozen=True)
class Plan:
    action: str
    title: str
    path: Path
    local: Local
    item: Item | None
    mode: int | None
    remote: bytes | None = None


def push_mode(local: Local, item: Item | None, override: int | None) -> int:
    """The mode a pushed item's tag carries.

    On Windows `st_mode` carries no POSIX permission, so the tag is never derived from it there.
    """
    if override is not None:
        return override
    if not IS_WINDOWS:
        assert local.mode is not None
        return local.mode & 0o777
    if item is not None:
        return mode_of(item)
    raise Refusal(
        "on Windows a new item needs --mode, since no POSIX mode can be read from the file"
    )


def warn_carriage_return(title: str, data: bytes) -> None:
    if b"\r" not in data or b"\0" in data:
        return
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return
    sys.stderr.write(
        f"warning: {title} is text carrying a carriage return, which a consumer may read as part of the value\n"
    )


def plan_line(plan: Plan) -> str:
    remote = stamp(plan.item.updated) if plan.item else "-"
    mode = "-" if plan.mode is None else f"{plan.mode:04o}"
    return (
        f"  {plan.action:<9} {plan.title}  local {stamp(plan.local.mtime)}  remote {remote}"
        f"  sha {short(plan.local.data)}  mode {mode}"
    )


def push_plans(
    a: argparse.Namespace, op: Op, home: Path, vault: Vault | None, items: dict[str, Item]
) -> list[Plan]:
    override = int(a.mode, 8) if a.mode is not None else None
    if a.paths:
        titles = [title_for(home, p) for p in a.paths]
        if len({t.casefold() for t in titles}) != len(titles):
            raise Refusal("two paths name one title, or titles that differ only in case")
        for title in titles:
            for existing in items:
                if existing != title and existing.casefold() == title.casefold():
                    raise Refusal(f"{title} collides case-insensitively with the item {existing!r}")
    elif vault is None:
        raise Refusal(f"no vault is named {vault_name(a.bucket)!r}, so name the files to push")
    else:
        titles = sorted(items)
    plans: list[Plan] = []
    for title in titles:
        refuse_symlinks(home, title)
        path = local_path(home, title)
        local = read_local(path)
        if local.data is None:
            if a.paths:
                raise Refusal(f"{title} does not exist under {home}")
            print(f"  skip      {title}  absent locally")
            continue
        if not local.data and not a.allow_empty:
            raise Refusal(f"{title} is empty, so pass --allow-empty to push it")
        warn_carriage_return(title, local.data)
        item = items.get(title)
        mode = push_mode(local, item, override)
        if item is None or vault is None:
            plans.append(Plan("create", title, path, local, None, mode))
            continue
        remote = download(op, vault, item)
        if remote == local.data:
            if mode_or_none(item) == mode:
                print(f"  same      {title}")
            else:
                plans.append(Plan("retag", title, path, local, item, mode, remote))
            continue
        assert local.mtime is not None
        if item.updated > local.mtime and not a.overwrite_newer:
            if a.paths:
                raise Refusal(
                    f"{title}: remote {stamp(item.updated)} is newer than local {stamp(local.mtime)},"
                    " so pass --overwrite-newer to replace it"
                )
            print(
                f"  skip      {title}  remote {stamp(item.updated)} is newer than local {stamp(local.mtime)},"
                " so pass --overwrite-newer to replace it"
            )
            continue
        plans.append(Plan("update", title, path, local, item, mode, remote))
    return plans


def create_vault(op: Op, bucket: str) -> Vault:
    """Create the bucket's vault, re-listing after a failure since the create may have landed."""
    name = vault_name(bucket)
    try:
        op.call(["vault", "create", name], write=True, detail="")
    except Execution:
        if find_vault(list_vaults(op), bucket) is None:
            raise
        sys.stderr.write(
            f"op vault create reported a failure, yet {name} is listed, so it is used as created\n"
        )
    if op.dry_run:
        return Vault("<new vault>", name)
    vault = find_vault(list_vaults(op), bucket)
    if vault is None:
        raise Execution(f"{name} is not listed after op vault create")
    return vault


def verify(op: Op, vault: Vault, plan: Plan, expected_id: str | None) -> None:
    """Read the item back and compare its id, its mode tag, and its bytes with what the plan read."""
    item = members(op, vault).get(plan.title)
    if item is None:
        raise Execution(f"{plan.title} is not listed after the write")
    if expected_id is not None and item.id != expected_id:
        raise Execution(f"{plan.title} is listed as a different item after an in-place edit")
    assert plan.mode is not None
    tags = [t for t in item.tags if t.startswith("mode:")]
    if tags != [mode_tag(plan.mode)]:
        raise Execution(
            f"{plan.title} carries mode tags {tags} after the write, not [{mode_tag(plan.mode)!r}]"
        )
    data = download(op, vault, item)
    if data != plan.local.data:
        raise Execution(
            f"{plan.title} reads back as {short(data)} where the plan read {short(plan.local.data)}, so check the file and the item"
        )


def push_one(op: Op, vault: Vault, plan: Plan) -> None:
    assert plan.mode is not None
    if plan.action == "create":
        args = ["document", "create", str(plan.path), "--vault", vault.id, "--title", plan.title]
        try:
            op.call(
                [*args, "--tags", tags_with_mode(None, plan.mode)], write=True, detail=plan.title
            )
        except Execution:
            if plan.title not in members(op, vault):
                raise
            sys.stderr.write(
                f"op document create reported a failure, yet {plan.title} is listed, so it is verified\n"
            )
        if not op.dry_run:
            verify(op, vault, plan, None)
        return
    assert plan.item is not None
    if plan.action == "update":
        op.call(
            ["document", "edit", plan.item.id, str(plan.path), "--vault", vault.id],
            write=True,
            detail=plan.title,
        )
    op.call(
        [
            "item",
            "edit",
            plan.item.id,
            "--vault",
            vault.id,
            "--tags",
            tags_with_mode(plan.item, plan.mode),
        ],
        write=True,
        detail=plan.title,
    )
    if not op.dry_run:
        verify(op, vault, plan, plan.item.id)


def cmd_push(a: argparse.Namespace, op: Op) -> int:
    home = home_dir()
    vault = find_vault(list_vaults(op), a.bucket)
    items = members(op, vault) if vault else {}
    plans = push_plans(a, op, home, vault, items)
    if not plans:
        print("Nothing to push.")
        return 0
    name = vault_name(a.bucket)
    print(f"Plan for {name}{'' if vault else ', a vault this push creates'}:")
    for plan in plans:
        print(plan_line(plan))
    if not op.dry_run:
        confirm(op, a.yes, f"Push {len(plans)} change(s) to {name}?")
    if vault is None:
        vault = create_vault(op, a.bucket)
    for index, plan in enumerate(plans, 1):
        sys.stderr.write(f"[{index}/{len(plans)}] {plan.action} {plan.title}\n")
        push_one(op, vault, plan)
    if not op.dry_run:
        print(f"Pushed and read back {len(plans)} item(s).")
    return 0


def check_restore_home(home: Path) -> None:
    """Refuse a restore root that is absent, a link, or not a directory.

    Off Windows it must also be exactly mode 0700, a check NTFS permissions cannot answer.
    """
    try:
        st = os.lstat(home)
    except FileNotFoundError as exc:
        raise Refusal(f"--home {home} does not exist") from exc
    if is_link(home) or not stat.S_ISDIR(st.st_mode):
        raise Refusal(f"--home {home} is not a directory")
    if not IS_WINDOWS and stat.S_IMODE(st.st_mode) != 0o700:
        raise Refusal(
            f"--home {home} is mode {stat.S_IMODE(st.st_mode):04o}, and a restore root must be 0700"
        )


def ensure_parents(home: Path, title: str) -> None:
    """Create each missing parent directory at 0700, never changing one that exists."""
    current = home
    for part in title.split("/")[:-1]:
        current = current / part
        if not current.exists():
            os.mkdir(current, 0o700)


def write_secret(path: Path, data: bytes, mode: int | None) -> None:
    """Write through a temporary file created exclusively at 0600 beside `path`, then rename it over.

    The mode is set on the open descriptor before the rename, so the file never appears with another.
    The temporary file is removed in `finally`, which also runs on SIGINT and SIGTERM.
    """
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=TEMP_PREFIX, suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def cmd_pull(a: argparse.Namespace, op: Op) -> int:
    if a.home is not None:
        home = Path(os.path.abspath(a.home))
        check_restore_home(home)
    else:
        home = home_dir()
    plans: list[Plan] = []
    for vault, items in selected(op, list_vaults(op), a.bucket):
        for title, item in sorted(items.items()):
            local, remote, mode, state = inspect(op, home, vault, title, item)
            path = local_path(home, title)
            if state == "same":
                print(f"  same      {title}")
                continue
            if (
                state == "differs"
                and local.mtime
                and local.mtime > item.updated
                and not a.overwrite_newer
            ):
                print(
                    f"  skip      {title}  local {stamp(local.mtime)} is newer than remote {stamp(item.updated)},"
                    " so pass --overwrite-newer to replace it"
                )
                continue
            action = {"only-remote": "create", "differs": "overwrite", "mode-differs": "chmod"}[
                state
            ]
            plans.append(Plan(action, title, path, local, item, mode, remote))
    if not plans:
        print("Nothing to pull.")
        return 0
    print(f"Plan for {home}:")
    for plan in plans:
        print(plan_line(plan))
    if op.dry_run:
        for plan in plans:
            print(f"  would write: {plan.path}")
        return 0
    changes = [p for p in plans if p.action != "create"]
    if changes:
        confirm(
            op, a.yes, f"Replace or re-mode {len(changes)} existing local file(s)?", then_op=False
        )
    for index, plan in enumerate(plans, 1):
        sys.stderr.write(f"[{index}/{len(plans)}] {plan.action} {plan.title}\n")
        if read_local(plan.path) != plan.local:
            raise Refusal(
                f"{plan.title} changed locally after the plan was printed, so it was not written"
            )
        ensure_parents(home, plan.title)
        assert plan.remote is not None
        if plan.action == "chmod":
            assert plan.mode is not None
            os.chmod(plan.path, plan.mode)
        else:
            write_secret(plan.path, plan.remote, plan.mode)
        if plan.path.read_bytes() != plan.remote:
            raise Execution(f"{plan.title} reads back differently from what was written")
    print(f"Wrote {len(plans)} file(s) under {home}.")
    return 0


def cmd_archive(a: argparse.Namespace, op: Op) -> int:
    title = title_for(home_dir(), a.path)
    vault = find_vault(list_vaults(op), a.bucket)
    if vault is None:
        raise Refusal(f"no vault is named {vault_name(a.bucket)!r}")
    item = members(op, vault).get(title)
    if item is None:
        raise Refusal(f"{vault.name} holds no item titled {title!r}")
    print(f"Plan for {vault.name}:\n  archive   {title}  remote {stamp(item.updated)}")
    if not op.dry_run:
        confirm(op, a.yes, f"Archive {title} in {vault.name}?")
    args = ["item", "delete", item.id, "--vault", vault.id, "--archive"]
    try:
        op.call(args, write=True, detail=title)
    except Execution:
        landed = members(op, vault).get(title)
        if landed is not None and landed.id == item.id:
            raise
        sys.stderr.write(f"op item delete reported a failure, yet {title} is no longer listed\n")
    if op.dry_run:
        return 0
    landed = members(op, vault).get(title)
    if landed is not None and landed.id == item.id:
        raise Execution(f"{title} is still listed after op item delete --archive")
    print(f"Archived {title} from {vault.name}.")
    return 0


class Measured(typing.NamedTuple):
    name: str
    holds: bool | None
    detail: str


SELFTEST_CONTENT = {
    "trailing-newline": b"alpha-selftest-value\n",
    "no-trailing-newline": b"alpha-selftest-value",
    "crlf": b"alpha-line-one\r\nalpha-line-two\r\n",
    "nul-bytes": b"alpha\x00selftest\x00\xff\x01",
}


def listed(op: Op, vault: Vault, title: str, after: str) -> Item:
    item = members(op, vault).get(title)
    if item is None:
        raise Execution(f"{title} is not listed after op {after}")
    return item


def measure_fields(op: Op, vault: Vault, results: list[Measured]) -> None:
    """Read the raw listing's field names before any parse, so a missing field is reported."""
    raw = op.json(
        ["item", "list", "--vault", vault.id, "--categories", "Document", "--format", "json"]
    )
    keys = sorted(raw[0]) if isinstance(raw, list) and raw and isinstance(raw[0], dict) else []
    results.append(
        Measured("item list carries 'updated_at'", "updated_at" in keys, f"row keys {keys}")
    )
    results.append(Measured("item list carries 'tags'", "tags" in keys, f"row keys {keys}"))


def selftest_cases(op: Op, vault: Vault, scratch: Path, results: list[Measured]) -> None:
    """Measure each `op` behavior the sync relies on, with constructed content only."""
    created: dict[str, Item] = {}
    for label, content in SELFTEST_CONTENT.items():
        path = scratch / label
        path.write_bytes(content)
        title = f"{SECRETS_DIR}/selftest_{label}"
        op.call(
            [
                "document",
                "create",
                str(path),
                "--vault",
                vault.id,
                "--title",
                title,
                "--tags",
                "mode:0600",
            ],
            write=True,
            detail=title,
        )
        if not created:
            measure_fields(op, vault, results)
        item = listed(op, vault, title, "document create")
        created[label] = item
        back = download(op, vault, item)
        detail = "byte-exact" if back == content else f"{len(content)} bytes became {len(back)}"
        results.append(Measured(f"document round-trip, {label}", back == content, detail))
    first = created["trailing-newline"]
    results.append(
        Measured(
            "create sets --tags",
            first.tags == ("mode:0600",),
            f"tags read back as {list(first.tags)}",
        )
    )
    op.call(
        ["item", "edit", first.id, "--vault", vault.id, "--tags", "mode:0640"],
        write=True,
        detail=first.title,
    )
    retagged = listed(op, vault, first.title, "item edit")
    results.append(
        Measured(
            "item edit --tags replaces",
            retagged.tags == ("mode:0640",),
            f"tags read back as {list(retagged.tags)}",
        )
    )
    time.sleep(2)
    edited = scratch / "edited"
    edited.write_bytes(b"alpha-selftest-value-2\n")
    op.call(
        ["document", "edit", first.id, str(edited), "--vault", vault.id],
        write=True,
        detail=first.title,
    )
    after = listed(op, vault, first.title, "document edit")
    results.append(
        Measured(
            "document edit keeps the item id",
            after.id == first.id,
            "same id" if after.id == first.id else "new id",
        )
    )
    results.append(
        Measured(
            "updated_at advances on document edit",
            after.updated > retagged.updated,
            f"{stamp(retagged.updated)} then {stamp(after.updated)}",
        )
    )
    back = download(op, vault, after)
    results.append(
        Measured(
            "document edit round-trip", back == b"alpha-selftest-value-2\n", f"sha {short(back)}"
        )
    )
    results.append(
        Measured(
            "document edit keeps the tags",
            after.tags == ("mode:0640",),
            f"tags read back as {list(after.tags)}",
        )
    )
    gone = created["crlf"]
    op.call(
        ["item", "delete", gone.id, "--vault", vault.id, "--archive"], write=True, detail=gone.title
    )
    still = members(op, vault).get(gone.title)
    results.append(
        Measured(
            "archived item leaves the listing",
            still is None,
            "absent" if still is None else "still listed",
        )
    )
    probe = op.probe(["document", "get", gone.id, "--vault", vault.id])
    results.append(Measured("document get on the archived item", None, f"exit {probe.returncode}"))


def cmd_selftest(a: argparse.Namespace, op: Op) -> int:
    bucket = f"selftest-{os.urandom(4).hex()}"
    name = vault_name(bucket)
    print(f"Plan: create the vault {name}, measure constructed documents in it, then delete it.")
    confirm(op, False, f"Create and delete {name}?")
    results: list[Measured] = []
    vault: Vault | None = None
    scratch = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        try:
            op.call(["vault", "create", name], write=True)
        finally:
            try:
                vault = find_vault(list_vaults(op), bucket)
            except BaseException:
                sys.stderr.write(
                    f"warning: {name} could not be listed, so delete it by hand if it exists\n"
                )
                raise
        if vault is None:
            raise Execution(f"{name} is not listed after op vault create")
        selftest_cases(op, vault, scratch, results)
    finally:
        if vault is not None:
            deleted = op.probe(["vault", "delete", vault.id])
            if deleted.returncode != 0:
                report_stderr(deleted.stderr)
                sys.stderr.write(
                    f"warning: op vault delete failed rc={deleted.returncode}, so delete {name} by hand\n"
                )
        for result in results:
            verdict = {True: "holds", False: "FAILS", None: "measured"}[result.holds]
            print(f"  {verdict:<9} {result.name}: {result.detail}")
        shutil.rmtree(scratch)
    return 1 if any(r.holds is False for r in results) else 0


def add_account(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--account",
        metavar="ACCOUNT",
        help="the op account, required where more than one is configured",
    )


def add_writes(p: argparse.ArgumentParser) -> None:
    p.add_argument("--yes", action="store_true", help="answer the confirmation prompt yes")
    p.add_argument(
        "--dry-run", action="store_true", help="print the plan and the op writes, and send none"
    )


def build_parser() -> argparse.ArgumentParser:
    ap = Parser(prog="secrets_sync.py", description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=Parser)

    p = sub.add_parser(
        "status", help="list the buckets, or compare named buckets with the home directory"
    )
    add_account(p)
    p.add_argument(
        "--bucket",
        action="append",
        metavar="BUCKET",
        help="compare this bucket's content, repeatable",
    )

    p = sub.add_parser("push", help="create or update a bucket's items from local files")
    add_account(p)
    add_writes(p)
    p.add_argument("--bucket", required=True, metavar="BUCKET")
    p.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="files to add or update, default every member that differs",
    )
    p.add_argument(
        "--overwrite-newer",
        action="store_true",
        help="push over a remote copy newer than the local file",
    )
    p.add_argument("--allow-empty", action="store_true", help="push an empty file")
    p.add_argument(
        "--mode", metavar="OCTAL", help="the mode tag to set, required on Windows for a new item"
    )

    p = sub.add_parser("pull", help="write a bucket's items under the home directory")
    add_account(p)
    add_writes(p)
    p.add_argument("--bucket", action="append", required=True, metavar="BUCKET", help="repeatable")
    p.add_argument("--home", metavar="DIR", help="restore under this mode-700 directory instead")
    p.add_argument(
        "--overwrite-newer",
        action="store_true",
        help="replace a local file newer than the remote copy",
    )

    p = sub.add_parser("archive", help="archive one item in a bucket")
    add_account(p)
    add_writes(p)
    p.add_argument("--bucket", required=True, metavar="BUCKET")
    p.add_argument("path", metavar="PATH")

    p = sub.add_parser(
        "selftest", help="measure op's behavior in a scratch vault this creates and deletes"
    )
    add_account(p)
    return ap


HANDLERS: dict[str, typing.Callable[[argparse.Namespace, Op], int]] = {
    "status": cmd_status,
    "push": cmd_push,
    "pull": cmd_pull,
    "archive": cmd_archive,
    "selftest": cmd_selftest,
}


def main(argv: Sequence[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    named = getattr(a, "bucket", None)
    for bucket in named if isinstance(named, list) else [] if named is None else [named]:
        if not BUCKET.fullmatch(bucket):
            ap.error(f"--bucket takes lowercase letters, digits, and hyphens, not {bucket!r}")
    if getattr(a, "home", None) == "":
        ap.error("--home takes a directory, not an empty string")
    if getattr(a, "mode", None) is not None and (
        not MODE_ARG.fullmatch(a.mode) or int(a.mode, 8) > 0o777
    ):
        ap.error(f"--mode takes an octal permission such as 0600, not {a.mode!r}")
    dry_run = bool(getattr(a, "dry_run", False))
    try:
        refuse_root()
        if a.cmd in WRITERS and not dry_run and not stdin_is_tty():
            raise Refusal(f"{a.cmd} writes, so it runs only with a terminal on stdin")
        op = Op(a.account, dry_run)
        with session(op):
            return HANDLERS[a.cmd](a, op)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except Execution as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"failed: unmodeled {type(exc).__name__}", file=sys.stderr)
        return 2


def run(argv: Sequence[str]) -> int | signal.Signals:
    """`main`, or the signal that interrupted it once every `finally` has released what it held."""
    received: list[int] = []

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        """Unwind on the first signal, passing over a later one that would cut the cleanup short."""
        if received:
            return
        received.append(signum)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
        signal.signal(signal.SIGINT, on_signal)
    try:
        return main(argv)
    except KeyboardInterrupt:
        signum = signal.Signals(received[0] if received else signal.SIGINT)
        print(f"secrets_sync: interrupted by {signum.name}.", file=sys.stderr)
        return signum


def reraise(signum: signal.Signals) -> None:
    """End this process by `signum`, so a caller sees the signal rather than an exit status."""
    with contextlib.suppress(OSError):
        sys.stdout.flush()
        sys.stderr.flush()
    if IS_WINDOWS:
        sys.exit(130)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    sys.exit(128 + signum)


if __name__ == "__main__":
    status = run(sys.argv[1:])
    if isinstance(status, signal.Signals):
        reraise(status)
    sys.exit(status)

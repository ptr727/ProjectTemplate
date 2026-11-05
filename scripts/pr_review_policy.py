"""A repository's pull request review policy: which bots review, on which bases, when, and how often.

Why this exists: the bots cost money and agent time per round. So how far each one is asked to
review is a per-repository choice rather than a fleet constant. The policy lives in the
`review` section of the repository's own `.github/agent-policy.json`. Changing it changes the
scripts' behavior for that repository alone, and a rollback is a revert of one file. A repository
carrying no file, or a file with no `review` section, reads the hub's
`spec/agent-policy.default.json`. That default matches the behavior the scripts had before the file
existed. The file's other sections belong to other readers, such as the host write guard's `guard`.

The trigger says how a review arrives, never whether the gate wants one. That is what lets the
skills stay silent about the trigger. `wait` sends each request the policy asks for, and the gate asks
the same question in every mode.

- `explicit`: the bot does not start on its own, so `wait` requests it once `when` allows. An
  explicit Copilot always gates, since `wait` requests Copilot only for a round the merge waits on.
- `auto`: the bot starts on its own. For Copilot, which the gate waits on, `wait` still requests
  it where nothing is pending. So a missed automatic trigger is filled rather than waited out. For
  CodeRabbit and Qodo `wait` requests nothing.
- `off`: the bot is never requested and never waited on.

The policy is read from the pull request's base branch rather than its head. So a pull request
cannot change the policy its own review runs under. A payload carries its policy under
`pr_review_payload.POLICY_KEY`, and a `bases` entry of `DEFAULT_BASE` names the default branch, whose name varies.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

POLICY_PATH = ".github/agent-policy.json"
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "spec" / "agent-policy.default.json"
VERSION = 1
SECTIONS = ("review", "guard")
BOTS = ("copilot", "coderabbit", "qodo")
TRIGGERS = ("auto", "explicit", "off")
WHENS = ("on-open", "after-checks")
ROUNDS = ("once", "per-push")
COMMANDS = ("review", "full review")
DEFAULT_BASE = "@default"
BOT_KEYS = frozenset({"trigger", "bases", "when", "rounds", "maxRounds", "gate", "command"})


class PolicyError(ValueError):
    """A policy that does not parse or breaks the schema, named with where it was read from."""


@dataclass(frozen=True)
class BotPolicy:
    """One bot's settings. `max_rounds` None means no cap."""

    trigger: str
    bases: tuple[str, ...]
    when: str
    rounds: str
    max_rounds: int | None
    gate: bool
    command: str

    def covers(self, base: str, default: str) -> bool:
        """Whether this bot reviews a pull request into `base`, `default` naming the default branch."""
        if self.trigger == "off":
            return False
        for entry in self.bases:
            if entry == DEFAULT_BASE:
                if default and base == default:
                    return True
            elif re.fullmatch(entry, base):
                return True
        return False

    def requests(self) -> bool:
        """Whether `wait` may send this bot a request at all, per the module docstring."""
        return self.trigger == "explicit" or (self.trigger == "auto" and self.gate)

    def capped(self) -> bool:
        """Whether the policy limits this bot's rounds on one pull request at all."""
        return self.rounds == "once" or self.max_rounds is not None

    def rounds_left(self, done: int, cut: bool = False) -> bool:
        """Whether a further round is allowed after `done` rounds on this pull request.

        A capped bot whose history was `cut` short has no round left, since what it spent is unknown.
        """
        if self.capped() and cut:
            return False
        if self.rounds == "once" and done >= 1:
            return False
        return self.max_rounds is None or done < self.max_rounds

    def never_waited_on(self) -> bool:
        """Whether the merge never waits on this bot, so no round of its is coming to settle on."""
        return self.trigger == "off" or not self.gate

    def waits_for_checks(self) -> bool:
        """Whether a request waits for the head's checks to go green first."""
        return self.trigger == "explicit" and self.when == "after-checks"


@dataclass(frozen=True)
class Policy:
    """Every bot's settings."""

    bots: dict[str, BotPolicy]

    def bot(self, name: str) -> BotPolicy:
        return self.bots[name]


def _fail(source: str, why: str) -> PolicyError:
    return PolicyError(f"{source}: {why}")


def _bot(source: str, name: str, raw: object) -> BotPolicy:
    where = f"bots.{name}"
    if not isinstance(raw, dict):
        raise _fail(source, f"{where} is not an object")
    stray = sorted(set(raw) - BOT_KEYS)
    if stray:
        raise _fail(source, f"{where} has unknown key(s) {', '.join(stray)}")
    missing = sorted({"trigger", "bases"} - set(raw))
    if missing:
        raise _fail(source, f"{where} is missing {', '.join(missing)}")
    trigger = raw["trigger"]
    if trigger not in TRIGGERS:
        raise _fail(source, f"{where}.trigger is {trigger!r}, not one of {', '.join(TRIGGERS)}")
    bases = raw["bases"]
    if not isinstance(bases, list) or not all(isinstance(b, str) and b for b in bases):
        raise _fail(source, f"{where}.bases is not a list of non-empty strings")
    for entry in bases:
        if entry != DEFAULT_BASE:
            try:
                re.compile(entry)
            except re.error as exc:
                raise _fail(
                    source, f"{where}.bases entry {entry!r} is not a regex ({exc})"
                ) from exc
    when = raw.get("when", "on-open")
    if when not in WHENS:
        raise _fail(source, f"{where}.when is {when!r}, not one of {', '.join(WHENS)}")
    rounds = raw.get("rounds", "per-push")
    if rounds not in ROUNDS:
        raise _fail(source, f"{where}.rounds is {rounds!r}, not one of {', '.join(ROUNDS)}")
    cap = raw.get("maxRounds")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 1):
        raise _fail(source, f"{where}.maxRounds is {cap!r}, not a positive integer or null")
    gate = raw.get("gate", name == "copilot")
    if not isinstance(gate, bool):
        raise _fail(source, f"{where}.gate is {gate!r}, not a boolean")
    if gate and name != "copilot":
        raise _fail(source, f"{where}.gate is true, and only copilot can gate the merge")
    if name == "copilot" and trigger == "explicit" and not gate:
        raise _fail(
            source,
            f"{where} is explicit with gate false, and wait requests Copilot only for a round the "
            "merge waits on, so an ungated Copilot is auto or off",
        )
    command = raw.get("command", "review")
    if "command" in raw and name != "coderabbit":
        raise _fail(source, f"{where}.command applies to coderabbit only")
    if command not in COMMANDS:
        raise _fail(source, f"{where}.command is {command!r}, not one of {', '.join(COMMANDS)}")
    return BotPolicy(trigger, tuple(bases), when, rounds, cap, gate, command)


def parse(data: object, source: str, fallback: bool = True) -> Policy:
    """The review policy `data` states, every bot named, or PolicyError naming the first fault.

    A file with no `review` section reads the hub default's, unless `fallback` is off, as it is for
    the hub default itself.
    """
    if not isinstance(data, dict):
        raise _fail(source, "the policy is not a JSON object")
    stray = sorted(set(data) - {"$schema", "version", *SECTIONS})
    if stray:
        raise _fail(source, f"unknown key(s) {', '.join(stray)}")
    if isinstance(data.get("version"), bool) or data.get("version") != VERSION:
        raise _fail(source, f"version is {data.get('version')!r}, and this reader knows {VERSION}")
    if "review" not in data and fallback:
        return load_default()
    review = data.get("review")
    if not isinstance(review, dict):
        raise _fail(source, "review is not an object")
    stray = sorted(set(review) - {"bots"})
    if stray:
        raise _fail(source, f"review has unknown key(s) {', '.join(stray)}")
    bots = review.get("bots")
    if not isinstance(bots, dict):
        raise _fail(source, "bots is not an object")
    stray = sorted(set(bots) - set(BOTS))
    if stray:
        raise _fail(source, f"bots names unknown bot(s) {', '.join(stray)}")
    missing = [b for b in BOTS if b not in bots]
    if missing:
        raise _fail(source, f"bots is missing {', '.join(missing)}, and every bot is stated")
    return Policy({name: _bot(source, name, bots[name]) for name in BOTS})


def load_default(path: Path = DEFAULT_PATH) -> Policy:
    """The hub's default policy, read from this checkout."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _fail(str(path), f"unreadable ({exc})") from exc
    return parse(data, f"hub default {path.name}", fallback=False)


def decode_contents(content: str, source: str) -> Policy:
    """The policy in a contents API `content` field, which is base64 with line breaks."""
    try:
        text = base64.b64decode(content, validate=False).decode("utf-8")
        data = json.loads(text)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail(source, f"does not decode as JSON ({exc})") from exc
    return parse(data, source)


Reader = Callable[[str, str, str, str], str | None]


def fetch(owner: str, repo: str, ref: str, read: Reader) -> Policy:
    """The policy `owner/repo` carries at `ref`, or the hub default where it carries none.

    `read` returns the contents API `content` field, or None where GitHub answered that the file is
    absent. It raises on any other failure, so an unread file never reads as an absent one.
    """
    source = f"{owner}/{repo}:{POLICY_PATH}@{ref}"
    content = read(owner, repo, POLICY_PATH, ref)
    if content is None:
        return load_default()
    return decode_contents(content, source)


def contents_reader(
    rest: Callable[[str, str], subprocess.CompletedProcess],
    absent: Callable[[subprocess.CompletedProcess], bool],
) -> Reader:
    """A policy reader over `rest`, one retried REST read with a jq projection, and `absent`.

    The projection emits the content as a JSON string, so a read that retries until it parses stops
    at the first good one. A file GitHub says is absent reads None. Any other failure ends the run.
    So a policy that could not be read is never taken for the hub default. That would quietly run a
    repository under rules it opted out of.
    """

    def read(owner: str, repo: str, path: str, ref: str) -> str | None:
        proc = rest(
            f"repos/{owner}/{repo}/contents/{path}?ref={quote(ref, safe='')}", ".content | tojson"
        )
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 and absent(proc):
            return None
        try:
            content = json.loads(out) if proc.returncode == 0 else None
        except json.JSONDecodeError:
            content = None
        if isinstance(content, str) and content:
            return content
        detail = (proc.stderr or out or "no output").strip()[:300]
        raise SystemExit(f"review policy: {owner}/{repo} {path} at {ref} was not read ({detail})")

    return read


@functools.cache
def policy_for(
    owner: str,
    repo: str,
    base: str,
    rest: Callable[[str, str], subprocess.CompletedProcess],
    absent: Callable[[subprocess.CompletedProcess], bool],
) -> Policy:
    """The review policy a pull request into `base` runs under, read once per run.

    Read from the base rather than the head, so a pull request cannot change its own policy.
    """
    try:
        return fetch(owner, repo, base, contents_reader(rest, absent))
    except PolicyError as exc:
        raise SystemExit(f"review policy: {exc}") from exc

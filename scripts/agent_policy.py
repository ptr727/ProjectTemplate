#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["editorconfig>=0.17", "ruamel.yaml>=0.18", "tomlkit>=0.13"]
# ///
"""Render a repository's agent policy from the registry, derive the bot-side settings, write and check them.

The registry is the one place a repository's review policy is chosen.
This script renders `.github/agent-policy.json` from `spec/agent-policy.default.json` plus the entry's optional `agentPolicy` override.
It derives the settings the bots read from their own files and the Copilot ruleset rule from that render.
`rulesets` composes the branch ruleset payloads from that rule and the entry's pullRequestCheck.
`write` puts the three files into a checkout and never stages or commits.
`check` reads them back from the repository at a ref and reports drift.

The libraries behind `write` and `check` are imported inside those subcommands.
So `render`, `facts` and `rulesets` run, and spec/validate.py imports `render`, with the standard library alone.

Exit codes: 0 ok, 1 a refusal, drift or a missing file, 2 the command could not run.
`check` exits 2 when any file could not be read, after printing every file's line.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gh_graphql
import pr_review_policy as policy_lib

SCHEMA_URL = (
    "https://raw.githubusercontent.com/ptr727/ProjectTemplate/main/spec/agent-policy.schema.json"
)
YAML_PATH = ".coderabbit.yaml"
TOML_PATH = ".pr_agent.toml"
MODELS = ("release", "operational")
CONFIG_DIR = Path(__file__).resolve().parents[1] / "repo-config"
MANIFEST_PATH = Path(__file__).resolve().parents[1] / "spec" / "files.json"


class Refusal(ValueError):
    """A policy or a target this script will not act on, with the reason."""


class Unreadable(RuntimeError):
    """A file the contents API could not be read, as opposed to one it says is absent."""


Reader = Callable[[str], "str | None"]


def entry_for(registry_data: Any, name: str) -> dict[str, Any]:
    """The registry entry named `name`, or an empty one where there is none."""
    repos = registry_data.get("repos", []) if isinstance(registry_data, dict) else []
    for entry in repos:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return {}


def override_bots(entry: dict[str, Any], name: str) -> dict[str, Any]:
    """The entry's per-bot override, shape-checked, or an empty one where it declares none."""
    if "agentPolicy" not in entry:
        return {}
    where = f"{name}: agentPolicy"
    declared = entry["agentPolicy"]
    if not isinstance(declared, dict) or set(declared) - {"review"}:
        raise Refusal(f"{where} must be an object holding only 'review'")
    review = declared.get("review", {})
    if not isinstance(review, dict) or set(review) - {"bots"}:
        raise Refusal(f"{where}.review must be an object holding only 'bots'")
    bots = review.get("bots", {})
    if not isinstance(bots, dict):
        raise Refusal(f"{where}.review.bots must be an object")
    stray = sorted(set(bots) - set(policy_lib.BOTS))
    if stray:
        raise Refusal(f"{where}.review.bots names unknown bot(s) {', '.join(stray)}")
    for bot, override in bots.items():
        if not isinstance(override, dict):
            raise Refusal(f"{where}.review.bots.{bot} must be an object")
    return bots


def render(registry_data: Any, name: str, model: str) -> dict[str, Any]:
    """The policy file for `name`, or a Refusal naming the repo and the fault."""
    default = json.loads(policy_lib.DEFAULT_PATH.read_text(encoding="utf-8"))
    overrides = override_bots(entry_for(registry_data, name), name)
    bots = {
        bot: {**raw, **overrides.get(bot, {})} for bot, raw in default["review"]["bots"].items()
    }
    guard: dict[str, Any] = {}
    if model == "operational":
        guard = {"primaryCheckout": {"commitInPlace": {"branches": ["develop"]}}}
    rendered = {"$schema": SCHEMA_URL, "version": default["version"], "review": {"bots": bots}}
    rendered["guard"] = guard
    try:
        parsed = policy_lib.parse(rendered, f"{name}: agentPolicy", fallback=False)
    except policy_lib.PolicyError as exc:
        raise Refusal(str(exc)) from exc
    crab = parsed.bot("coderabbit")
    if crab.trigger == "auto" and policy_lib.DEFAULT_BASE not in crab.bases:
        raise Refusal(
            f"{name}: agentPolicy coderabbit is auto without {policy_lib.DEFAULT_BASE} in bases, "
            "and CodeRabbit's automatic review always includes the default branch"
        )
    qodo = parsed.bot("qodo")
    if qodo.trigger == "auto" and not all(
        qodo.covers(branch, "main") for branch in ("main", "develop")
    ):
        raise Refusal(
            f"{name}: agentPolicy qodo is auto without covering both main and develop, "
            "and Qodo's automatic review covers every pull request, so it cannot stop at fewer"
        )
    return rendered


def anchored(base: str) -> str:
    """`base` as a CodeRabbit pattern, which searches, where a policy base must match whole.

    An alternation like `^a|b$` anchors each branch alone, so it is wrapped rather than trusted.
    """
    body = base.removesuffix("$")
    slashes = len(body) - len(body.rstrip("\\"))
    if base.startswith("^") and base.endswith("$") and slashes % 2 == 0 and "|" not in base:
        return base
    return f"^(?:{base})$"


def serialize(rendered: dict[str, Any]) -> str:
    return json.dumps(rendered, indent=4) + "\n"


def carried_globs(manifest_path: Path = MANIFEST_PATH) -> list[str]:
    """A glob per tree spec/files.json carries verbatim, which a carrier's reviewers skip.

    Such a tree is the hub's text byte for byte, so a finding on it belongs at the hub's source.
    Every such tree is listed whatever its appliesTo, since skipping a path a repository lacks is harmless.
    """
    trees = json.loads(manifest_path.read_text(encoding="utf-8"))["trees"]
    return [f"{tree['target']}/**" for tree in trees if tree["fidelity"] == "verbatim-tree"]


def copilot_rule(rendered: dict[str, Any]) -> dict[str, bool]:
    """Whether the Copilot ruleset rule belongs on `main` and on `develop` under a rendered policy."""
    copilot = policy_lib.parse(rendered, "rendered policy", fallback=False).bot("copilot")
    auto = copilot.trigger == "auto"
    return {
        "main": auto and copilot.covers("main", "main"),
        "develop": auto and copilot.covers("develop", "main"),
    }


def facts(rendered: dict[str, Any]) -> dict[str, Any]:
    """The settings the bots and the Copilot ruleset rule take from a rendered policy."""
    parsed = policy_lib.parse(rendered, "rendered policy", fallback=False)
    crab = parsed.bot("coderabbit")
    globs = carried_globs()
    return {
        "copilotRule": copilot_rule(rendered),
        "coderabbit": {
            "enabled": crab.trigger == "auto",
            "base_branches": [anchored(b) for b in crab.bases if b != policy_lib.DEFAULT_BASE],
            "auto_pause_after_reviewed_commits": 0,
            "path_filters": [f"!{glob}" for glob in globs],
        },
        "qodo": {
            "disable_auto_feedback": parsed.bot("qodo").trigger != "auto",
            "ignore_glob": globs,
        },
    }


def ruleset_sources(model: str, config_dir: Path = CONFIG_DIR) -> dict[str, Path]:
    """The committed payload each branch's ruleset is composed from under `model`."""
    develop = "operational/develop.json" if model == "operational" else "develop.json"
    return {"main": config_dir / "main.json", "develop": config_dir / develop}


def rulesets(
    registry_data: Any, name: str, model: str, config_dir: Path = CONFIG_DIR
) -> dict[str, dict[str, Any]]:
    """Each branch's ruleset payload under `model`, composed for the repository `name`.

    The Copilot rule follows the render, taking its shape from the `main` payload.
    The required status check rule is dropped where the entry's pullRequestCheck is false.
    repo-config/configure.sh applies and checks these, and spec/audit.py expects them.
    """
    sources = ruleset_sources(model, config_dir)
    payloads = {
        branch: json.loads(path.read_text(encoding="utf-8")) for branch, path in sources.items()
    }
    copilot = [r for r in payloads["main"]["rules"] if r.get("type") == "copilot_code_review"]
    wanted = copilot_rule(render(registry_data, name, model))
    if not copilot and any(wanted.values()):
        raise Refusal(
            f"{name}: the policy wants a Copilot rule, and {sources['main']} carries none to copy"
        )
    keep_check = entry_for(registry_data, name).get("pullRequestCheck") is not False
    gated = ["main"] if model == "operational" else ["main", "develop"]
    for branch in gated if keep_check else []:
        if not any(r.get("type") == "required_status_checks" for r in payloads[branch]["rules"]):
            raise Refusal(
                f"{name}: the required status check is kept, and {sources[branch]} carries none"
            )
    for branch, payload in payloads.items():
        rules = [r for r in payload["rules"] if r.get("type") != "copilot_code_review"]
        if wanted[branch]:
            rules += copilot
        if not keep_check:
            rules = [r for r in rules if r.get("type") != "required_status_checks"]
        payload["rules"] = rules
    return payloads


def load_registry(path: str) -> Any:
    """The registry JSON, or None where the file is absent."""
    target = Path(path)
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refusal(f"registry {path} is unreadable ({exc})") from exc


def git_out(checkout: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise Refusal(f"{checkout} is not a git work tree ({proc.stderr.strip()})")
    return proc.stdout.strip()


def verify_checkout(checkout: Path, repo: str) -> None:
    """Refuse unless `checkout` is a work tree whose origin is `repo`."""
    if git_out(checkout, "rev-parse", "--is-inside-work-tree") != "true":
        raise Refusal(f"{checkout} is not a git work tree")
    top = git_out(checkout, "rev-parse", "--show-toplevel")
    if Path(top).resolve() != checkout.resolve():
        raise Refusal(
            f"{checkout} is inside the work tree {top}, and the top level is the only target"
        )
    url = git_out(checkout, "remote", "get-url", "origin")
    if gh_graphql.origin_slug(url) != repo.lower():
        raise Refusal(f"{checkout} has origin {url!r}, which is not {repo}")


def current_text(path: Path) -> str | None:
    """The file's text with its line endings untouched, or None where it is absent."""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", newline="")


def lf_text(path: Path) -> str | None:
    """The file's text with CRLF read as LF, or None where it is absent.

    The YAML and TOML round-trippers keep a carriage return inside a comment and emit LF elsewhere.
    So they parse LF text, and plan() writes CRLF back wherever the file was CRLF.
    """
    text = current_text(path)
    return None if text is None else text.replace("\r\n", "\n")


def take_tail_comment(seq: Any) -> Any:
    """Detach and return the comment hanging off a block list's last item, or None.

    A comment after a block list hangs off that item, so an item appended after it would move the comment above itself.
    """
    return seq.ca.items.pop(len(seq) - 1, None) if len(seq) else None


def extend_keeping_comments(seq: Any, items: list[Any]) -> None:
    """Append `items` to a block list, keeping each comment where the reader left it.

    An end-of-line comment stays on its own item, and a comment on the lines after the list stays before the next key.
    """
    held = take_tail_comment(seq)
    last = len(seq) - 1
    seq.extend(items)
    if held is None:
        return
    token = held[0]
    if token is None or not token.value.startswith("#"):
        seq.ca.items[len(seq) - 1] = held
        return
    eol, _, rest = token.value.partition("\n")
    token.value = eol + "\n"
    seq.ca.items[last] = held
    if rest.strip():
        after = copy.copy(token)
        after.value = "\n" + rest
        seq.ca.items[len(seq) - 1] = [after, None, None, None]


def set_base_branches(node: Any, items: list[str]) -> None:
    """Make node['base_branches'] hold `items`, double-quoted, keeping the comment that follows the list."""
    from ruamel.yaml.comments import CommentedSeq
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString

    quoted = [DoubleQuotedScalarString(item) for item in items]
    seq = node.get("base_branches")
    if not isinstance(seq, CommentedSeq):
        node["base_branches"] = CommentedSeq(quoted)
        return
    held = take_tail_comment(seq)
    del seq[:]
    seq.extend(quoted)
    if held is None:
        return
    if quoted:
        seq.ca.items[len(quoted) - 1] = held
        return
    # An empty list has no item to carry it, so the comment becomes full-line comments before the next key.
    # Where no key follows, it moves onto the key's own entry.
    token = held[0]
    seq.fa.set_flow_style()
    keys = list(node.keys())
    following = keys[keys.index("base_branches") + 1 :]
    if following:
        lines = [ln.strip() for ln in token.value.split("\n") if ln.strip()]
        text = "\n".join(ln[2:] if ln.startswith("# ") else ln[1:] for ln in lines)
        node.yaml_set_comment_before_after_key(following[0], before=text, indent=token.column)
        return
    token.value = token.value.lstrip("\n").lstrip(" ")
    token.column = 0
    node.ca.items["base_branches"] = [None, None, token, None]


def missing(got: Any, required: list[str]) -> list[str] | None:
    """The required entries `got` lacks, or None where `got` is not a list at all.

    A repository may list entries of its own, so only the required ones are asserted.
    """
    if got is None:
        return list(required)
    if not isinstance(got, list):
        return None
    return [item for item in required if item not in got]


def exactly(got: Any, want: bool | int) -> bool:
    """Whether `got` is `want` in value and type, so YAML's `false` never reads as a pause of 0."""
    if isinstance(want, bool):
        return isinstance(got, bool) and got == want
    return isinstance(got, int) and not isinstance(got, bool) and got == want


def plan_yaml(path: Path, want: dict[str, Any]) -> str | None:
    """The new .coderabbit.yaml text, or None where it already holds the wanted values.

    Only reviews.auto_review.enabled, base_branches and auto_pause_after_reviewed_commits are set,
    and reviews.path_filters gains any required exclusion it lacks.
    So every other key, list entry and comment stays.
    """
    from io import StringIO

    from ruamel.yaml import YAML, YAMLError
    from ruamel.yaml.comments import CommentedMap

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    existing = lf_text(path)
    try:
        doc = yaml.load(existing) if existing is not None else None
    except YAMLError as exc:
        raise Refusal(f"{path} does not parse as YAML ({exc})") from exc
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, dict):
        raise Refusal(f"{path} is not a YAML mapping")
    node = doc
    for key in ("reviews", "auto_review"):
        if node.get(key) is None:
            node[key] = CommentedMap()
        node = node[key]
        if not isinstance(node, dict):
            raise Refusal(f"{path}: {key} is not a mapping")
    reviews = doc["reviews"]
    filters_absent = missing(reviews.get("path_filters"), want["path_filters"])
    if filters_absent is None:
        raise Refusal(f"{path}: reviews.path_filters is not a list")
    branches = list(want["base_branches"])
    pause = want["auto_pause_after_reviewed_commits"]
    enabled_ok = exactly(node.get("enabled"), want["enabled"])
    pause_ok = exactly(node.get("auto_pause_after_reviewed_commits"), pause)
    same = enabled_ok and pause_ok and node.get("base_branches") == branches and not filters_absent
    if existing is not None and same:
        return None
    if not enabled_ok:
        node["enabled"] = want["enabled"]
    if node.get("base_branches") != branches or "base_branches" not in node:
        set_base_branches(node, branches)
    if not pause_ok:
        node["auto_pause_after_reviewed_commits"] = pause
    if filters_absent:
        from ruamel.yaml.comments import CommentedSeq
        from ruamel.yaml.scalarstring import DoubleQuotedScalarString

        if reviews.get("path_filters") is None:
            reviews["path_filters"] = CommentedSeq()
        extend_keeping_comments(
            reviews["path_filters"], [DoubleQuotedScalarString(item) for item in filters_absent]
        )
    out = StringIO()
    yaml.dump(doc, out)
    return out.getvalue()


def plan_toml(path: Path, want: dict[str, Any]) -> str | None:
    """The new .pr_agent.toml text, or None where it already holds the wanted values.

    Only config.disable_auto_feedback is set, and ignore.glob gains any required glob it lacks.
    """
    import tomlkit
    from tomlkit.exceptions import TOMLKitError

    existing = lf_text(path)
    try:
        doc = tomlkit.parse(existing) if existing is not None else tomlkit.document()
    except TOMLKitError as exc:
        raise Refusal(f"{path} does not parse as TOML ({exc})") from exc
    if "config" not in doc:
        doc["config"] = tomlkit.table()
    config = doc["config"]
    if not isinstance(config, dict):
        raise Refusal(f"{path}: config is not a table")
    ignore = doc.get("ignore")
    if ignore is not None and not isinstance(ignore, dict):
        raise Refusal(f"{path}: ignore is not a table")
    globs_absent = missing(None if ignore is None else ignore.get("glob"), want["ignore_glob"])
    if globs_absent is None:
        raise Refusal(f"{path}: ignore.glob is not a list")
    value = want["disable_auto_feedback"]
    feedback_ok = exactly(config.get("disable_auto_feedback"), value)
    if existing is not None and feedback_ok and not globs_absent:
        return None
    if not feedback_ok:
        config["disable_auto_feedback"] = value
    if globs_absent:
        if ignore is None:
            doc["ignore"] = ignore = tomlkit.table()
        if ignore.get("glob") is None:
            ignore["glob"] = tomlkit.array()
        ignore["glob"].extend(globs_absent)
    return tomlkit.dumps(doc)


def plan(rendered: dict[str, Any], checkout: Path, repo: str) -> list[tuple[str, str | None]]:
    """Every target's new text, or None where unchanged, computed before anything is written.

    A refusal on any file therefore leaves the checkout untouched.
    """
    verify_checkout(checkout, repo)
    want = facts(rendered)
    policy_file = checkout / policy_lib.POLICY_PATH
    policy = keep_endings(policy_file, serialize(rendered))
    return [
        (policy_lib.POLICY_PATH, None if current_text(policy_file) == policy else policy),
        (
            YAML_PATH,
            keep_endings(checkout / YAML_PATH, plan_yaml(checkout / YAML_PATH, want["coderabbit"])),
        ),
        (
            TOML_PATH,
            keep_endings(checkout / TOML_PATH, plan_toml(checkout / TOML_PATH, want["qodo"])),
        ),
    ]


def wants_crlf(path: Path) -> bool:
    """Whether `path` keeps CRLF: an existing file by its own endings, a new one by its .editorconfig."""
    existing = current_text(path)
    if existing is not None:
        return "\r\n" in existing
    import editorconfig

    try:
        props = editorconfig.get_properties(str(path.resolve()))
    except editorconfig.EditorConfigError as exc:
        raise Refusal(f"{path}: the .editorconfig governing it does not parse ({exc})") from exc
    return props.get("end_of_line") == "crlf"


def keep_endings(path: Path, text: str | None) -> str | None:
    """`text` with the line endings `path` keeps, so a write never flips a file's endings."""
    if text is None or not wants_crlf(path):
        return text
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def apply(
    checkout: Path,
    steps: list[tuple[str, str | None]],
    dry_run: bool = False,
    remove: tuple[str, ...] = (),
) -> list[str]:
    """Write each planned text and delete each path in `remove`, returning one status line per path.

    A planned text of None, or a removal whose path is already absent, reports `unchanged`.
    A dry run reports `would write` or `would remove` and touches nothing.
    """
    lines = []
    for path, text in steps:
        if text is None:
            lines.append(f"unchanged {path}")
        elif dry_run:
            lines.append(f"would write {path}")
        else:
            target = checkout / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="")
            lines.append(f"wrote {path}")
    for path in remove:
        target = checkout / path
        if not target.exists():
            lines.append(f"unchanged {path}")
        elif dry_run:
            lines.append(f"would remove {path}")
        else:
            target.unlink()
            lines.append(f"removed {path}")
    return lines


def write(rendered: dict[str, Any], checkout: Path, repo: str, dry_run: bool = False) -> list[str]:
    """Write the three files into `checkout`, returning one status line per file.

    A dry run plans only and reports `would write` in place of `wrote`.
    """
    return apply(checkout, plan(rendered, checkout, repo), dry_run)


def gh_rest(
    path: str, jq: str, run: gh_graphql.Runner = gh_graphql.run_subprocess
) -> subprocess.CompletedProcess[str]:
    """One REST read through `gh`, returned whole since a 404 is an answer rather than a failure."""
    code, out, err = run(["gh", "api", path, "--jq", jq])
    return subprocess.CompletedProcess(["gh", "api", path], code, out, err)


def gh_reader(repo: str, ref: str, run: gh_graphql.Runner = gh_graphql.run_subprocess) -> Reader:
    """A reader of one file's text through the contents API, None where it is absent."""
    owner, name = repo.split("/", 1)
    read = policy_lib.contents_reader(
        lambda path, jq: gh_rest(path, jq, run), lambda proc: gh_graphql.said_absent(proc.stderr)
    )

    def get(path: str) -> str | None:
        try:
            content = read(owner, name, path, ref)
        except SystemExit as exc:
            raise Unreadable(str(exc)) from exc
        if content is None:
            return None
        try:
            return base64.b64decode(content).decode("utf-8")
        except (ValueError, binascii.Error) as exc:
            raise Unreadable(f"{path} did not decode ({exc})") from exc

    return get


class Result(NamedTuple):
    """One assertion's outcome. `unreadable` marks a file the API could not be read, which is no drift."""

    ok: bool
    msg: str
    unreadable: bool = False


def check_file(
    path: str, read: Reader, judge: Callable[[str], list[tuple[bool, str]]]
) -> list[Result]:
    try:
        text = read(path)
    except Unreadable as exc:
        return [Result(False, f"{path} is unreadable, not missing ({exc})", True)]
    if text is None:
        return [Result(False, f"{path} is missing, run configure.sh apply --checkout to write it")]
    return [Result(ok, msg) for ok, msg in judge(text)]


def judge_policy(rendered: dict[str, Any]) -> Callable[[str], list[tuple[bool, str]]]:
    def judge(text: str) -> list[tuple[bool, str]]:
        try:
            same = json.loads(text) == rendered
        except ValueError:
            return [(False, f"{policy_lib.POLICY_PATH} is not JSON")]
        msg = f"{policy_lib.POLICY_PATH} equals the policy rendered from the registry"
        return [(same, msg if same else msg.replace("equals", "differs from"))]

    return judge


def judge_yaml(want: dict[str, Any]) -> Callable[[str], list[tuple[bool, str]]]:
    def judge(text: str) -> list[tuple[bool, str]]:
        from ruamel.yaml import YAML, YAMLError

        try:
            doc = YAML(typ="safe").load(text)
        except YAMLError:
            return [(False, f"{YAML_PATH} does not parse as YAML")]
        auto = ((doc or {}).get("reviews") or {}).get("auto_review") or {}
        branches = auto.get("base_branches")
        pause = want["auto_pause_after_reviewed_commits"]
        enabled_ok = exactly(auto.get("enabled"), want["enabled"])
        branches_ok = isinstance(branches, list) and set(branches) == set(want["base_branches"])
        pause_ok = exactly(auto.get("auto_pause_after_reviewed_commits"), pause)
        filters_absent = missing(
            ((doc or {}).get("reviews") or {}).get("path_filters"), want["path_filters"]
        )
        return [
            (enabled_ok, f"{YAML_PATH} reviews.auto_review.enabled is {want['enabled']}"),
            (
                branches_ok,
                f"{YAML_PATH} reviews.auto_review.base_branches is {want['base_branches']}",
            ),
            (
                pause_ok,
                f"{YAML_PATH} reviews.auto_review.auto_pause_after_reviewed_commits is {pause}",
            ),
            (
                filters_absent == [],
                f"{YAML_PATH} reviews.path_filters includes {want['path_filters']}",
            ),
        ]

    return judge


def judge_toml(want: dict[str, Any]) -> Callable[[str], list[tuple[bool, str]]]:
    def judge(text: str) -> list[tuple[bool, str]]:
        import tomllib

        msg = f"{TOML_PATH} config.disable_auto_feedback is {want['disable_auto_feedback']}"
        try:
            doc = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return [(False, f"{TOML_PATH} does not parse as TOML")]
        got = (doc.get("config") or {}).get("disable_auto_feedback")
        ignore = doc.get("ignore")
        table = ignore is None or isinstance(ignore, dict)
        globs_ok = table and missing((ignore or {}).get("glob"), want["ignore_glob"]) == []
        return [
            (exactly(got, want["disable_auto_feedback"]), msg),
            (globs_ok, f"{TOML_PATH} ignore.glob includes {want['ignore_glob']}"),
        ]

    return judge


def check(rendered: dict[str, Any], read: Reader) -> list[Result]:
    """Every assertion the repository's three files owe against the render."""
    want = facts(rendered)
    results = check_file(policy_lib.POLICY_PATH, read, judge_policy(rendered))
    results += check_file(YAML_PATH, read, judge_yaml(want["coderabbit"]))
    results += check_file(TOML_PATH, read, judge_toml(want["qodo"]))
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_policy.py",
        description="Render, write, and check a repository's agent policy files.",
        epilog="Exit codes: 0 ok, 1 a refusal, drift or a missing file, 2 the command or a file read could not run.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd, text in (
        ("render", "print .github/agent-policy.json"),
        ("facts", "print the bot-side settings derived from the render"),
        ("rulesets", "write the main and develop ruleset payloads composed for the repository"),
        ("write", "write the three files into a checkout, never staging or committing"),
        ("check", "compare the repository's files at a ref with the render"),
    ):
        one = sub.add_parser(cmd, help=text, description=text)
        one.add_argument("--registry", required=True, help="path to registry/repos.json")
        one.add_argument("--name", required=True, help="the registry entry name")
        one.add_argument("--model", required=True, choices=MODELS)
        if cmd in ("write", "check"):
            one.add_argument("--repo", required=True, help="owner/name")
        if cmd == "write":
            one.add_argument("--checkout", required=True, help="the repository checkout")
            one.add_argument(
                "--dry-run", action="store_true", help="plan every file and write none"
            )
        if cmd == "check":
            one.add_argument("--ref", required=True, help="the branch or commit to read")
        if cmd == "rulesets":
            one.add_argument("--out-dir", required=True, help="where main.json and develop.json go")
    return parser


def run(args: argparse.Namespace) -> int:
    registry_data = load_registry(args.registry)
    rendered = render(registry_data, args.name, args.model)
    if args.command == "render":
        sys.stdout.write(serialize(rendered))
    elif args.command == "facts":
        print(json.dumps(facts(rendered)))
    elif args.command == "rulesets":
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for branch, payload in rulesets(registry_data, args.name, args.model).items():
            (out_dir / f"{branch}.json").write_text(
                json.dumps(payload, indent=4) + "\n", encoding="utf-8"
            )
    elif args.command == "write":
        for line in write(rendered, Path(args.checkout), args.repo, args.dry_run):
            print(line)
    else:
        results = check(rendered, gh_reader(args.repo, args.ref))
        for result in results:
            print(f"  {'ok  ' if result.ok else 'FAIL'} {result.msg}")
        if any(result.unreadable for result in results):
            return 2
        return 0 if all(result.ok for result in results) else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Refusal as exc:
        print(f"agent_policy.py: {exc}", file=sys.stderr)
        return 1
    except (OSError, ImportError, gh_graphql.GraphQLError) as exc:
        print(f"agent_policy.py: could not run ({exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

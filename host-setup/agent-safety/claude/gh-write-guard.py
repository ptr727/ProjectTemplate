#!/usr/bin/env python3
"""PreToolUse guard: deny the GitHub-write footguns, the primary-checkout mutation, and the unbounded wait behind three incidents.

Registered as a Claude Code PreToolUse hook on the Bash tool. It reads the tool-input JSON on stdin,
classifies the command, and DENIES (with a reason shown to the agent) when a command is a GitHub *write*
matching a known-dangerous pattern, a mutating git operation run directly against a primary checkout, or a
shell wait carrying no bound. Reads and everything that is not a clear write pass through. See host-setup/agent-safety/README.md for
the requirements this implements, stated once, agent-agnostic, and for how to audit this file against
them.

Precision over recall for the write-footgun shapes (1-3), the primary-checkout shape (6), and the
unbounded-wait shape (7): they deny the specific shapes that caused an incident, not everything unparseable, since a false deny would break the
agent, and a miss still falls under the GOVERNANCE.md "Repository Boundaries and Write Safety" prose
rules. The branch-bypass rule (4) instead fails CLOSED on the protected-by-default branches, because the
harm there is a silent success under the maintainer's admin bypass. The denied shapes:

  1. a state-changing gh call whose output is discarded or forced to success
     (>/dev/null, 2>/dev/null, &>/dev/null, || true, || :, || echo)
  2. a GraphQL mutation passing a literal GitHub node id (PRRT_/PR_/BOT_/...) instead of a $variable
  3. a gh write with an explicit -R/--repo/repos/<owner>/<repo> target under an owner other than the
     checkout origin's, unless the maintainer granted that target in GH_WRITE_GUARD_ALLOW. Sibling
     repositories under the same owner are allowed, since the harm this guards is reaching a stranger's
     repository, not working across your own fleet in one session.
  4. a git operation that would only land by bypassing an active branch rule: a direct push to a branch
     whose rules require a pull request, a force-push where history is protected, a delete where deletion
     is blocked, or an explicit-bypass flag (`gh pr merge --admin`, `git commit/push --no-verify`, or a
     `-c`/`--config-env` override of `core.hooksPath` on a commit or push). The
     branch's live rules are the judge, so a code-style develop is denied and a config-style develop is
     allowed with no hardcoded repo list.
  5. a hand-rolled reply/resolve for a review thread: a `resolveReviewThread` mutation via `gh api
     graphql`, or a POST to the review-comment replies endpoint, where `scripts/pr_review.py reply ...
     --resolve` is the documented one-call path. Splitting the two into separate hand-run acts is what
     let a reply sit unresolved across a push and a re-request, reading as untriaged to a maintainer
     skimming the pull request (the incident behind this rule). Permitted only under the same
     GH_WRITE_GUARD_ALLOW grant rule 3 reads, since the helper refuses a cross-owner pull request outright
     and the hand-run GraphQL form is then the documented fallback, not a footgun.
  6. a mutating git operation (checkout/switch/pull/reset/rebase/merge/cherry-pick/revert/restore/stash
     (anything but list/show)/clean -f/add/commit/rm/mv/apply/am/push/worktree remove -f) run
     directly against a primary checkout, not a linked worktree. This is the harm behind a
     separate incident, where an agent reused the maintainer's own primary checkout instead of a
     worktree twice despite having read the prose rule against it. A "primary checkout" is decided
     by comparing `git rev-parse --git-dir` against `--git-common-dir`, never a `.git`-is-a-directory
     guess (a submodule's `.git` is a file and is still primary). The target directory follows real
     git's own priority rather than a last-option-wins scan: any `-C <dir>` options on the
     invocation compose sequentially onto a leading `cd` (inside a `sh -c`/`bash -c` wrapper too) or
     the session's own cwd, then an explicit `--work-tree`/`GIT_WORK_TREE=` value, when given, wins
     over that result regardless of `-C`, and `--git-dir`/`GIT_DIR=` alone never relocates that
     reported target, matching git's own fallback. A leading `export GIT_WORK_TREE=x GIT_DIR=y &&`
     prefix is read the same way an inline `VAR=x git ...` prefix already is, since a real shell
     export persists into the following command exactly as effectively, confirmed live to discard a
     tracked local modification with no redirect at all on the git invocation itself, a shape the
     inline-prefix scan alone cannot see.
     Whether the invocation is primary-checkout at all is a separate question from the mutation target,
     though: an explicit `--git-dir`/`GIT_DIR=` is resolved and tested for primary-checkout-ness
     directly, regardless of `--work-tree`, since `--git-dir` names the repository actually mutated,
     confirmed live that `--git-dir=<primary>/.git --work-tree=<empty-dir>` mutates `<primary>` even
     though `<empty-dir>` resolves as no git repository at all, which testing the work-tree value alone
     would fail open on. `~`/`$HOME` is expanded throughout and a relative value is joined against the
     running result. Checkout/switch force flags (`-b`/`-B` for checkout, `-c`/`-C` for switch,
     `-f`/`--force`/`--discard-changes`/`--orphan` for either) are recognized bundled or attached into a
     short-option cluster (`-qf`, `-Bname`, `-Cother`), not only as an exact token. A subcommand this
     rule does not recognize is resolved through a bounded chain of git aliases (inline `-c
     alias.<name>=`, then the target's own persisted config) before falling through to allow. A
     `!`-prefixed shell alias is denied outright rather than interpreted. Exempt: `worktree
     add/list/prune` and an unforced `worktree remove` (the
     documented way to use a primary checkout at all), `merge --ff-only`/`pull --ff-only` (can never
     discard anything), and a `checkout <ref>`/`switch <ref>` carrying no force-oriented flag whose
     argument actually resolves as a ref, verified live (git's own ref-switch path refuses to carry a
     local modification, but its pathspec-restore fallback for an argument that is not a ref, such as
     `checkout .` or `checkout -- <path>`, carries no such check and is denied). A non-force flag such
     as `--detach`/`-q` alongside the ref stays exempt too, since it changes nothing about git's own
     overwrite-refusal, verified live, so admitting it widens no actual safety hole, only the exemption's
     literal shape, matching the documented base-clone cleanup step in the repo-worktree skill. Granted
     only by GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT (a recognized falsy
     value such as "0"/"false" reads as not granted, not as any-non-empty-string-is-truthy), the same
     channel shape as GH_WRITE_GUARD_ALLOW.
  7. a shell wait carrying no bound: a `while`/`until` compound whose body calls `sleep`, with neither a
     `timeout <duration>` running the `sh -c`/`bash -c` wrapper that holds it nor an arithmetic guard in
     its own condition. The placement matters: `timeout` takes a command and a loop keyword is not one. This is the harm behind a third incident,
     where seven such loops outlived the subagents that started them, the run that dispatched those
     subagents, and every worktree it had already retired, each forking a fresh `sleep` every half minute
     against a condition that could never become true, until they were killed by PID by hand. A shell
     started by a tool call runs in its own session, so it survives the agent that started it and nothing
     reaps it. A heredoc body is data rather than a command line and is skipped, except one fed to a
     shell, which is the script that shell runs. A line opening several heredocs is read as opening
     its first alone and, where every body closes, as opening each in order through the last one
     that is data, keeping whole a body fed to a shell before it. A line holding `((` or `$[` beside
     a `<<` is read as opening nothing, and as opening each `<<` whose tag it accepts alone and
     together with every later one whose body closes, wherever that reading closes its first body,
     ends on a body that is data, and removes at least one line. Any reading holding an unbounded
     wait denies. A command with more readings than the rule builds
     is denied unread when it names both `sleep` and a loop keyword.

Run `gh-write-guard.py --selftest` to verify the decision matrix without Claude Code.
"""

import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
import unicodedata
from urllib.parse import quote, urlsplit

# --- What counts as a GitHub write -------------------------------------------------------------------
# The gh subcommands that mutate.
# A path-qualified or `.exe`-suffixed `gh` still starts a shell word this matches, the same recognition `_is_gh_exe` gives it for argv-position parsing.
# The `gh api` command is handled separately, in `_is_gh_write`, since it needs argv-aware method and GraphQL-query inspection rather than a fixed subcommand list.
_GH_WRITE_SUB = re.compile(
    r"""\bgh(?:\.exe)?\s+(?:
        pr\s+(?:create|comment|close|merge|edit|review|reopen|ready|lock|unlock)
      | issue\s+(?:create|comment|close|edit|reopen|delete|lock|unlock|pin|unpin|transfer)
      | release\s+(?:create|edit|delete|upload)
      | repo\s+(?:create|delete|edit|rename|archive)
      | (?:label|secret|variable|ruleset)\s+(?:create|delete|edit|set)
      | gist\s+(?:create|edit|delete)
    )\b""",
    re.VERBOSE,
)
_GRAPHQL = re.compile(r"\bgh(?:\.exe)?\s+api\b.*\bgraphql\b", re.DOTALL)
_MUTATION = re.compile(r"\bmutation\b")
# Loose pre-filter only: matches `git` before `push` even with global options between them
# (git -C <dir> push). _push_arg_lists is the accurate arbiter that confirms an executable push.
_GIT_PUSH = re.compile(r"\bgit\b.*?\bpush\b", re.DOTALL)

# --- Bypass-of-branch-rule detectors (Rule 4) --------------------------------------------------------
# A git operation is denied when it would only succeed by bypassing an active branch rule.
# The harm is that the maintainer's admin identity can bypass, so a plain-looking push silently lands on a protected branch.
# The judgment is made against the branch's *live* rules, which makes it self-configuring: a code-style develop carries `pull_request` and is denied, where a config-style develop does not and is allowed.
# The exception is the explicit-bypass flags below, which are the bypass by definition and need no query.
#
# Branches that fail CLOSED when their rules cannot be read - protected-by-default across every config.
_PROTECTED_DEFAULT_ORDER = ("main", "master", "develop")
_PROTECTED_DEFAULT = set(_PROTECTED_DEFAULT_ORDER)
# `gh pr merge --admin` overrides required reviews/status checks with admin power.
_GH_ADMIN_MERGE = re.compile(r"\bgh(?:\.exe)?\s+pr\s+merge\b[^\n|&;]*(?:^|\s)--admin\b")

# --- Risk-pattern detectors --------------------------------------------------------------------------
# Output-discard and force-success tails.
# A bare `2>&1` is deliberately not here, since it merges stderr into stdout and leaves the output visible, so it is not suppression, and denying it would break `... 2>&1 | tee log`.
_SUPPRESS = re.compile(r">\s*/dev/null|&>\s*/dev/null|2>\s*/dev/null|\|\|\s*(?:true\b|echo\b|:)")
# A quoted argument value, in either double or single quotes.
# It is stripped before the suppression scan so a --body or --title that merely mentions `|| true` or `>/dev/null` as text is not mistaken for a real command tail.
# Real suppression tails are unquoted shell operators, so stripping quotes never hides an actual footgun.
# The double-quoted form allows `\"` escapes so an embedded quote does not end the span early.
# Shell single quotes take no escapes, so their form is literal.
_QUOTED_SPAN = re.compile(r'"(?:\\.|[^"\\])*"' r"|'[^']*'")
# A GitHub global node id literal, being an uppercase prefix such as PR_, PRRT_, IC_ or BOT_ followed by a long base64url body, or a legacy MD-prefixed base64 id.
# The uppercase prefix plus a body of at least 12 characters keeps it from matching an ordinary underscored word in a reply body, such as body="fixed_the_thing_now" with its lowercase prefix.
_NODE_ID_LITERAL = re.compile(r"^(?:[A-Z]{1,5}_[A-Za-z0-9_\-]{12,}|MD[A-Za-z0-9]{12,})$")
# -F/-f name=VALUE, capturing the value - handles "quoted" and bare
_FIELD_ASSIGN = re.compile(
    r"""(?:-F|-f|--field|--raw-field)\s+[A-Za-z_][\w]*=(?P<v>'[^']*'|"[^"]*"|\S+)"""
)
# Every spelling gh accepts for the target flag, being `--repo x`, `--repo=x`, `-R x`, `-R=x`, and the attached short form `-Rx`.
# A form left out is not a near-miss, it is a silent bypass of the whole repository scope, so each is read by argv position below (`_gh_write_targets`) rather than assumed to be a space-separated pair.
_REPO_FLAG_BARE = {"--repo", "-R"}
# `gh api` accepts a leading slash on the path (`gh api /repos/o/r/...`), so it is optional here too.
_REPOS_PATH_TOKEN = re.compile(r"^/?repos/(?P<owner>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+)")
# Flags whose own value is opaque text (a PR/issue title, body, or notes), and so is skipped whole rather than pattern-matched for a repo target.
# Without this, a --body describing a `--repo <owner>/<repo>` doc line, or a commit message quoting the same convention, reads as a real flag.
# Shared across every create/comment/edit-style subcommand (pr, issue, release, gist).
_GH_CREATE_TEXT_VALUE_FLAGS = {
    "--title",
    "-t",
    "--body",
    "-b",
    "--body-file",
    "-F",
    "--notes",
    "--notes-file",
    "--message",
    "-m",
    "--desc",
}
# `gh api`'s own value-taking flags, meaningful only inside an `api` invocation.
# `-f` alone is the boolean `--fill` on `gh pr create`, so it must not be treated as value-consuming outside of `api`, or the flag right after it (a real `--repo <owner>/<repo>`) is silently skipped.
# `-F` is value-taking either way (`--body-file` on create, `--field` on api), so it stays shared.
_GH_API_VALUE_FLAGS = _GH_CREATE_TEXT_VALUE_FLAGS | {
    "-f",
    "-F",
    "--field",
    "--raw-field",
    "--input",
    "--jq",
    "--template",
    "-q",
    "-H",
    "--header",
    "--method",
    "-X",
    "--cache",
    "--hostname",
    "-p",
    "--preview",
}


def _is_gh_write(cmd):
    """True when `cmd` is a GitHub write: a known-mutating `gh` subcommand, a `git push`, or a `gh api`
    call whose effective method is not GET. A GraphQL call is a write only when its query is a mutation,
    or when its body is supplied by `--input` and so cannot be read at all.
    """
    # Argv-aware for the `gh api` half, reading a flag or a GraphQL query only from where it actually sits in one invocation's own argv, not a raw substring search over the whole command.
    # A substring search reads a write-method spelling out of an opaque flag value too, such as a
    # `--jq` expression that merely contains the text `-XPOST` as data, misclassifying a harmless read.
    if _GH_WRITE_SUB.search(cmd) or _push_arg_lists(cmd):
        return True
    for args in _all_gh_arg_lists(cmd):
        if not args or args[0] != "api":
            continue
        path = _gh_api_path(args)
        if path == "graphql":
            # --input checked before trusting any -f/-F query=... value.
            # A -f/-F field becomes a URL query-string parameter rather than a body field whenever --input is also present.
            # A harmless-looking inline query alongside --input therefore has no effect on the actual request, and the real body is the uninspectable input file.
            if _gh_has_input(args):
                return True  # uninspectable body, treated cautiously so rules 1-5 can look closer
            q = _gh_graphql_query(args)
            if q:
                if _MUTATION.search(q):
                    return True
                continue  # a genuine read-only query, not a mutation
            continue
        if _gh_effective_method(args) != "GET":
            return True
    return False


def _origin_owner_repo(cwd):
    try:
        url = subprocess.run(
            ["git", "-C", cwd or ".", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        ).stdout.strip()
    # A checkout with no usable origin is answered as unknown rather than crashing the hook.
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"[:/]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$", url)
    return (m.group(1).lower(), m.group(2).lower()) if m else None


_ALLOW_ENV = "GH_WRITE_GUARD_ALLOW"


def _granted_targets(environ=None):
    """Maintainer-granted write targets, as {(owner, repo)} with repo '*' meaning any repo of that owner.

    Read from the environment the agent's session was launched with, which is the one channel the agent
    cannot set for itself: a hook runs as its own process, so an inline `VAR=x cmd` prefix or an `export`
    in a Bash call never reaches here. Granting is therefore a deliberate maintainer act taken outside the
    session, not something an agent can do to get past a block it just hit.
    """
    out = set()
    raw = (environ if environ is not None else os.environ).get(_ALLOW_ENV, "")
    for tok in re.split(r"[,\s]+", raw):
        if "/" not in tok:
            continue
        owner, repo = tok.split("/", 1)
        owner, repo = owner.strip().lower(), repo.strip().lower()
        if owner and repo:
            out.add((owner, repo))
    return out


def _target_permitted(target, origin, granted):
    """True when a write to target is in scope for a checkout whose origin is origin."""
    # Same owner covers the origin itself and every sibling repository, which is the case the maintainer works in daily.
    # A different owner is the incident shape and needs the grant.
    if target[0] == origin[0]:
        return True
    return target in granted or (target[0], "*") in granted


def _live_branch_rules(owner, repo, branch):
    """Return the set of active rule types on a branch, or None if the query cannot be resolved.

    None (not an empty set) signals "unknown" so the caller can fail closed on a protected-default
    branch. An empty set means the branch genuinely has no rules (a feature branch).
    """
    try:
        r = subprocess.run(
            # Quote the branch, since a name carrying `/`, such as feature/x, would otherwise split the API path.
            [
                "gh",
                "api",
                f"repos/{owner}/{repo}/rules/branches/{quote(branch, safe='')}",
                "--jq",
                "[.[].type]",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    # Any failure to query means unknown, so the caller can fail closed instead of the hook crashing.
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    try:
        return set(json.loads(r.stdout or "[]"))
    # Unparseable output also means unknown rather than a crash.
    except Exception:  # noqa: BLE001
        return None


def _current_push_branch(cwd):
    """Resolve the destination branch of a bare `git push` from the branch's configured push target."""
    for args in (
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}"],
        ["rev-parse", "--abbrev-ref", "HEAD"],
    ):
        try:
            r = subprocess.run(
                ["git", "-C", cwd or ".", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                # A ref name is bytes like a path, and a strict decode of one that is not UTF-8 raises into the handler below, which answers None, and None leaves `_check_push_bypass` with no target to test against the protected branches.
                errors="surrogateescape",
                timeout=5,
                check=False,
            )
        # The hook must answer on any failure rather than crash.
        except Exception:  # noqa: BLE001
            return None
        ref = r.stdout.strip()
        if r.returncode == 0 and ref and ref != "HEAD":
            return ref.split("/", 1)[1] if "/" in ref else ref
    return None


# Flags that consume the following token as a value, so the value is not a positional (remote/refspec).
_PUSH_VALUE_FLAGS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
# The git global options, which sit before the subcommand, that consume the following token as their value.
_GIT_GLOBAL_VALUE_OPTS = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
    "--config-env",
}


# A newline ends a command exactly as `;` does, so it is an operator character here rather than whitespace.
# Read as whitespace it vanishes when tokenizing, and every token on a later line of a multi-line command is then read as one more argument of the first line's command.
# A backslash-newline continuation is folded to a space in `classify` before any of this runs, so every newline reaching the tokenizer is a real command separator.
# The string is the form shlex takes the set in, and the set is derived from it so the two cannot drift apart.
_PUNCTUATION_CHARS = "();<>|&\n"
_SHELL_OP_CHARS = set(_PUNCTUATION_CHARS)
_EXTGLOB_PREFIX = "@?*+!"
_WORD_BREAK_CHARS = " \t\n;&|<>("
_CASE_KEYWORD = re.compile(r"(?:case|esac)(?=[ \t\n;&|<>()]|$)")
_COND_PRECEDERS = {"!", "if", "then", "elif", "else", "while", "until", "do", "time", "{"}
_DQ_ESCAPE = re.compile(r'\\([$`"\\\n])')
_COMMENT_SCAN_BAIL = ("\\", "`", "$(", "${", "$'", '$"', "<<", "((")
_EXTGLOB_OPEN = re.compile(r"[@?*+!]\(")


def _skip_backticks(cmd, i):
    """Index just past the backquote closing a command substitution whose body starts at `i`."""
    while i < len(cmd):
        if cmd[i] == "\\":
            i += 2
        elif cmd[i] == "`":
            return i + 1
        else:
            i += 1
    raise ValueError("unterminated backquote")


def _skip_quote_body(cmd, i, closer):
    """Index just past the `closer` ending a quoted span whose body starts at `i`, for a double-quoted
    or `$'...'` span. Both honor a backslash escape, and a double-quoted span also holds nested
    substitutions, each skipped whole, since a quote inside one does not end the outer span.
    """
    while i < len(cmd):
        ch = cmd[i]
        if ch == "\\":
            i += 2
        elif ch == closer:
            return i + 1
        elif closer == '"' and cmd.startswith("$$", i):
            i += 2
        elif closer == '"' and (ch == "`" or cmd.startswith(("$(", "${"), i)):
            i = _skip_nested(cmd, i)
        else:
            i += 1
    raise ValueError("unterminated quote")


_HEREDOC_IN_GROUP = re.compile(
    r"(?<!<)<<(?!<)(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z_][A-Za-z0-9_.\-]*))"
)


def _skip_heredoc_lines(cmd, i, heredocs):
    """Index just past the delimiter line of the last of `heredocs`, their bodies starting at `i`."""
    for tag, dash in heredocs:
        while i < len(cmd):
            end = cmd.find("\n", i)
            end = len(cmd) if end < 0 else end
            line = cmd[i:end]
            i = end + 1
            if (line.lstrip("\t") if dash else line) == tag:
                break
    return min(i, len(cmd))


def _skip_nested(cmd, i):
    """Index just past the quote, escape, or substitution opening at `i`, or None where none does."""
    ch = cmd[i]
    if ch == "\\":
        return i + 2
    if ch == "'":
        end = cmd.find("'", i + 1)
        if end < 0:
            raise ValueError("unterminated quote")
        return end + 1
    if ch == '"':
        return _skip_quote_body(cmd, i + 1, '"')
    if ch == "`":
        return _skip_backticks(cmd, i + 1)
    if cmd.startswith("$$", i):
        return i + 2
    if cmd.startswith("$((", i):
        return _skip_group(cmd, i + 2, "(", ")", comments=False)
    if cmd.startswith("$'", i):
        return _skip_quote_body(cmd, i + 2, "'")
    if cmd.startswith('$"', i):
        return _skip_quote_body(cmd, i + 2, '"')
    if cmd.startswith("$(", i):
        return _skip_group(cmd, i + 2, "(", ")", comments=True)
    if cmd.startswith("${", i):
        return _skip_group(cmd, i + 2, None, "}", comments=False)
    return None


def _skip_group(cmd, i, opener, closer, comments):
    """Index just past the `closer` ending a group whose body starts at `i`. Bash reads the body in a
    fresh quoting context, so a quote the outer text opened does not carry into it. With `comments`,
    as in a command substitution, a `#` opening a word hides the rest of its line, a `closer`
    included, and a heredoc's body is skipped line by line to its delimiter, so a quote in the body
    does not span past it. A parameter expansion, arithmetic, or an extglob pattern passes False,
    since a `#` there is text and a `<<` is no heredoc. Bash counts no nested `{` inside `${`, so
    a brace group passes None as its `opener`. A `)` ending a `case` pattern closes nothing.
    """
    depth = cases = 0
    pending = []
    while i < len(cmd):
        opened = _HEREDOC_IN_GROUP.match(cmd, i) if comments else None
        if opened:
            pending.append(
                (next(t for t in opened.group(2, 3, 4) if t is not None), bool(opened.group(1)))
            )
            i = opened.end()
            continue
        if cmd[i] == "\n" and pending:
            i = _skip_heredoc_lines(cmd, i + 1, pending)
            pending.clear()
            continue
        nxt = _skip_nested(cmd, i)
        if nxt is not None:
            i = nxt
            continue
        ch = cmd[i]
        word_start = cmd[i - 1] in _WORD_BREAK_CHARS
        if comments and word_start and _CASE_KEYWORD.match(cmd, i):
            cases += 1 if cmd.startswith("case", i) else -1 if cases else 0
            i += 4
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            if not depth and not (cases and closer == ")"):
                return i + 1
            depth -= 1 if depth else 0
        elif comments and ch == "#" and word_start:
            end = cmd.find("\n", i)
            if end < 0:
                break
            i = end
            continue
        i += 1
    raise ValueError("unterminated group")


def _arith_depth(run, depth, cmd, at, closers):
    """The arithmetic nesting after an operator `run` found at `cmd[at]`, None outside `(( ))`, else
    the open parens. A `((` whose group closes on a lone `)` is nested subshells, as bash reads it.
    `closers` is `_paren_closers(cmd)`.
    """
    k = 0
    while k < len(run):
        if depth is None:
            if run.startswith("((", k):
                if _closes_as_arithmetic(cmd, at + k + 2, closers):
                    depth = 0
                k += 2
                continue
        elif run[k] == "(":
            depth += 1
        elif run[k] == ")":
            if depth:
                depth -= 1
            elif run.startswith("))", k):
                depth = None
                k += 2
                continue
        k += 1
    return depth


def _paren_closers(cmd):
    """Map each `(` in `cmd` to the index of the `)` closing it, counting raw characters. Built in
    one pass per command, since a run of `((` asks once per pair and a scan per ask is quadratic.
    """
    closers, stack = {}, []
    for i, ch in enumerate(cmd):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            closers[stack.pop()] = i
    return closers


def _closes_as_arithmetic(cmd, i, closers):
    """True where the parens opened just before `cmd[i]` close together, on `))`, `closers` being
    `_paren_closers(cmd)`.
    """
    close = closers.get(i - 1)
    return close is None or cmd[close + 1 : close + 2] == ")"


def _heredoc_line_tokens(line):
    """Tokenize one heredoc body line. A `#` is text in a body, so it is read that way first. A body
    fed to a shell is a script, though, where a `#` does open a comment, so a line that does not parse
    as text is read again with comments, and then plainly, rather than dropped.
    """
    for comments in (False, True):
        try:
            return _context_lex(line, comments)
        except (ValueError, RecursionError):
            pass
    return _operator_tokens(line)


def _context_lex(cmd, comments=True):
    """Tokenize `cmd` as `_operator_lex` does, tracking the contexts bash nests rather than only plain
    quotes. Quote state carries across lines, a backslash escapes, and a substitution or `$'...'` span
    is read whole, so a quote inside `"$(...)"` does not end the outer one. A `#` opening a word
    outside every quote is a comment and dropped, with `comments`, and is text inside arithmetic, an
    extglob pattern, a `[[ =~ ]]` operand, and a heredoc body. A `#` right after any `)` is kept
    as text, which bash agrees with after a substitution's `)` and not after a subshell's. A `=~` operand is one word holding
    `|`, parens, and spaces inside parens, as bash reads it. A `!(` opening a word is a negated
    subshell rather than a pattern, since bash runs it so unless extglob is on. A `-` glued to `<<`
    is the dash form, where a spaced one is the delimiter itself. Raises ValueError where a quote or
    group never closes, which bash also rejects.
    """
    toks, word, heredocs = [], [], []
    started = quoted = False
    cmd_pos = True
    word_at = 0
    arith = None  # paren depth inside `(( ))`, None outside it
    closers = None  # `_paren_closers(cmd)`, built at the first operator run
    in_cond = False  # inside `[[ ]]`
    regex = None  # paren depth inside a `=~` operand, None outside one
    tag_wanted = None  # (index the `<<` ended at, dash form) until its delimiter word arrives

    def flush():
        nonlocal started, quoted, in_cond, regex, tag_wanted, cmd_pos
        if not started:
            return
        tok = "".join(word)
        bare = not quoted
        word.clear()
        started = quoted = False
        at_command = cmd_pos
        cmd_pos = at_command and bare and tok in _COND_PRECEDERS
        toks.append(tok)
        if tag_wanted is not None:
            end, dash = tag_wanted
            if word_at == end and tok.startswith("-"):
                dash, tok = True, tok[1:]
            if tok or not bare:
                heredocs.append((tok, dash))
                tag_wanted = None
            else:
                tag_wanted = (-1, dash)
        regex = 0 if in_cond and bare and tok == "=~" else None
        if tok == "[[" and bare and at_command:
            in_cond = True
        elif tok == "]]" and bare:
            in_cond = False

    n = len(cmd)
    i = 0
    while i < n:
        ch = cmd[i]
        if regex is not None and (
            ch in "(|<>#" or (ch == ")" and regex) or (regex and ch in " \t")
        ):
            regex += (ch == "(") - (ch == ")")
            if not started:
                word_at = i
            started = True
            word.append(ch)
            i += 1
            continue
        if ch in " \t":
            flush()
            i += 1
            continue
        if ch == "#" and comments and not started and arith is None and cmd[i - 1 : i] != ")":
            end = cmd.find("\n", i)
            i = n if end < 0 else end
            continue
        if (
            ch == "("
            and started
            and arith is None
            and word
            and word[-1] == cmd[i - 1]
            and cmd[i - 1] in _EXTGLOB_PREFIX
            and cmd[i - 2 : i - 1] != "\\"
            and not (cmd[i - 1] == "!" and word_at == i - 1)
        ):
            end = _skip_group(cmd, i + 1, "(", ")", comments=False)
            word.append(cmd[i:end])
            i = end
            continue
        if ch in _SHELL_OP_CHARS:
            flush()
            j = i
            while j < n and cmd[j] in _SHELL_OP_CHARS:
                j += 1
                if cmd[j - 1] == "\n" and heredocs:
                    break
            run = cmd[i:j]
            toks.append(run)
            regex = None
            if arith is None and "<<" in run and "<<<" not in run:
                tag_wanted = (j, False)
            if closers is None:
                closers = _paren_closers(cmd)
            arith = _arith_depth(run, arith, cmd, i, closers)
            cmd_pos = not _is_redir_op(run)
            i = j
            if "\n" in run:
                tag_wanted = None
                if heredocs:
                    i = _heredoc_bodies(cmd, i, heredocs, toks)
                    heredocs.clear()
            continue
        if ch == "\\" and cmd[i + 1 : i + 2] == "\n":
            i += 2
            continue
        if not started:
            word_at = i
        started = True
        if ch != "$" and ch in "\\'\"`":
            quoted = True
        if ch == "\\":
            if i + 1 >= n:
                raise ValueError("no escaped character")
            word.append(cmd[i + 1])
            i += 2
        elif cmd.startswith("$$", i):
            word.append("$$")
            i += 2
        elif ch == "'":
            end = _skip_nested(cmd, i)
            word.append(cmd[i + 1 : end - 1])
            i = end
        elif ch == '"' or cmd.startswith(('$"', "$'"), i):
            quoted = True
            body = i + (1 if ch == '"' else 2)
            end = _skip_quote_body(cmd, body, cmd[body - 1])
            text = cmd[body : end - 1]
            word.append(
                _DQ_ESCAPE.sub(lambda m: m.group(1).strip("\n"), text)
                if cmd[body - 1] == '"'
                else text
            )
            i = end
        elif ch == "`" or cmd.startswith("${", i):
            end = _skip_nested(cmd, i)
            word.append(cmd[i:end])
            i = end
        else:
            word.append(ch)
            i += 1
    flush()
    return toks


def _heredoc_bodies(cmd, i, heredocs, toks):
    """Tokenize the heredoc bodies starting at `i`, each through its delimiter line, into `toks`,
    and return the index just past the last one. Bash reads a body line by line rather than as part
    of the command, so each line is tokenized alone and a quote in one never spans into the next.
    """
    n = len(cmd)
    for tag, dash in heredocs:
        while i < n:
            end = cmd.find("\n", i)
            if end < 0:
                end = n
            line = cmd[i:end]
            toks.extend(_heredoc_line_tokens(line))
            if end < n:
                toks.append("\n")
            i = end + 1
            if (line.lstrip("\t") if dash else line) == tag:
                break
    return min(i, n)


def _operator_lex(text, posix=True):
    """Tokenize `text`, isolating operator runs, and raise where its quoting does not parse.

    `posix=False` keeps each token's quotes, so a caller can tell a quoted `";"` from a separator.
    """
    lex = shlex.shlex(text, posix=posix, punctuation_chars=_PUNCTUATION_CHARS)
    lex.whitespace_split = True
    # `shlex.shlex`'s own default keeps `#` as a comment starter, unlike `shlex.split()`, which explicitly clears it, and confirmed live to otherwise fuse `git fetch origin # x\ngit reset --hard` into one invocation, hiding the second command from every tokenizer-based rule.
    # Cleared unconditionally: a truncated command is a far worse failure than an ordinary `#` becoming literal trailing argv words instead.
    lex.commenters = ""
    lex.whitespace = lex.whitespace.replace(
        "\n", ""
    )  # A newline is an operator above rather than a gap between words.
    return list(lex)


def _operator_tokens(line):
    """Tokenize one line as the primary path does, isolating operator runs. A line that does not
    parse falls back to plain splitting, which keeps a quoted argument whole rather than cutting it
    at an operator character.
    """
    try:
        return _operator_lex(line)
    except (ValueError, TypeError):  # bad quoting, or punctuation_chars unsupported on old Python
        pass
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def _strip_comments(cmd):
    """Return `cmd` with every comment bash reads cut away, or None where it holds none or holds
    quoting, a heredoc, or a nested context such as arithmetic or an extglob pattern, where a `#`
    is text, that this scan does not model. Only plain quotes are tracked, and their state
    carries across lines as bash's does, so a `#` inside a quote an earlier line opened stays text.
    """
    if any(s in cmd for s in _COMMENT_SCAN_BAIL) or _EXTGLOB_OPEN.search(cmd):
        return None
    out = []
    quote = None
    comment = found = False
    for i, ch in enumerate(cmd):
        if comment:
            if ch != "\n":
                continue
            comment = False
        elif quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or cmd[i - 1] in " \t" or cmd[i - 1] in _SHELL_OP_CHARS):
            comment = found = True
            continue
        out.append(ch)
    return "".join(out) if found else None


def _base_tokens(cmd):
    """Split each line of `cmd` plainly, keeping a newline between lines."""
    toks = []
    for i, line in enumerate(cmd.split("\n")):
        if i:
            toks.append("\n")
        try:
            toks.extend(shlex.split(line, posix=True))
        except ValueError:
            toks.extend(line.split())
    return toks


def _line_fallback_tokens(cmd):
    """The reading that predates `_context_lex`: the command with its comments stripped, followed
    by each line split plainly, or where the strip does not apply, each line tokenized alone.
    """
    stripped = _strip_comments(cmd)
    if stripped is not None:
        try:
            return [*_operator_lex(stripped), "\n", *_base_tokens(cmd)]
        except (ValueError, TypeError):
            pass
    toks = []
    for i, line in enumerate(cmd.split("\n")):
        if i:
            toks.append("\n")
        toks.extend(_operator_tokens(line))
    return toks


def _shell_tokens(cmd):
    """Tokenize like a shell, isolating operator runs (`|`, `&&`, `;`, newline, `>`, `2>&1`, ...) as
    their own tokens even when glued to a word - so a `>` or a newline inside a quoted value stays part
    of that token while a real redirection or line break is separated. A command whose quoting shlex
    cannot follow, an apostrophe in a comment or a quote nested in `"$(...)"` being the usual cases,
    is read both by `_line_fallback_tokens` and by `_context_lex`, and the tokens of both are
    returned. The union means a context the scan misreads can never hide a command the line
    fallback saw, at the cost of the line fallback's false denies, such as loop text inside a
    quote an earlier line opened.
    """
    try:
        return _operator_lex(cmd)
    except (ValueError, TypeError):  # bad quoting, or punctuation_chars unsupported on old Python
        pass
    toks = _line_fallback_tokens(cmd)
    try:
        return [*toks, "\n", *_context_lex(cmd)]
    except (ValueError, RecursionError):
        return toks


def _is_shell_op(tok):
    return tok != "" and all(c in _SHELL_OP_CHARS for c in tok)


def _is_redir_op(tok):
    return _is_shell_op(tok) and (">" in tok or "<" in tok)  # >, >>, <, >&, &>


def _is_separator(tok):
    return _is_shell_op(tok) and ">" not in tok and "<" not in tok  # |, ||, &, &&, ;, (, ), newline


def _is_git_exe(tok):
    """True if the token invokes git, including an absolute/relative path or a .exe suffix
    (/usr/bin/git, ./git, C:\\...\\git.exe) - an exact "git" match alone is a bypass path.
    """
    base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    return base in ("git", "git.exe")


def _is_gh_exe(tok):
    """True if the token invokes gh, including an absolute/relative path or a .exe suffix, the same
    recognition `_is_git_exe` gives git, so an invocation named only inside a quoted --body forms no
    such token and is never mistaken for a real gh call.
    """
    base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    return base in ("gh", "gh.exe")


def _collect_arglist(toks, start):
    """Collect argv tokens from `start` up to the next shell separator (|, &&, ;, newline), skipping a
    redirection operator and the file-descriptor number or target token attached to it. Shared by
    `_git_subcommand_arglists` and `_gh_arg_lists` so a command's own argv, not text living inside an
    unrelated --body/--title/-f value elsewhere in the line, is what either scans for a target.

    Returns (args, index_after_this_invocation).
    """
    n = len(toks)
    k = start
    args = []
    while k < n:
        t = toks[k]
        if _is_separator(t):
            break  # a command separator ends this invocation
        if t.isdigit() and k + 1 < n and _is_redir_op(toks[k + 1]):
            k += 1  # a file-descriptor number before a redirection is shell syntax, not argv
            continue
        if _is_redir_op(t):
            k += 1  # skip the redirection operator and its target token; args continue after it
            if k < n and not _is_shell_op(toks[k]):
                k += 1
            continue
        args.append(t)
        k += 1
    return args, k


def _git_subcommand_arglists(cmd, sub):
    """Every `git [global-options] <sub>` in the command, each as the argv up to the next shell operator.

    Keying off a real `git`->`<sub>` token sequence (git's value-taking global options skipped, an
    absolute-path or .exe git recognized) means the same invocation named inside a quoted --body forms no
    such sequence, and a compound `<sub> A && <sub> B` yields two independent arg lists so both are seen,
    whether the two are joined by `&&` or written on their own lines.
    """
    return [args for _, args in _git_subcommand_invocations(cmd, sub)]


def _git_subcommand_invocations(cmd, sub):
    """Every `git [global-options] <sub>` in the command, as (global-option tokens, argv after <sub>)."""
    toks = _shell_tokens(cmd)
    n = len(toks)
    out = []
    i = 0
    while i < n:
        if not _is_git_exe(toks[i]):
            i += 1
            continue
        j = i + 1
        while j < n and toks[j].startswith("-"):
            if toks[j] in _GIT_GLOBAL_VALUE_OPTS and "=" not in toks[j]:
                j += 2  # this global option consumes the next token as its value
            else:
                j += 1
        if j < n and toks[j] == sub:
            args, k = _collect_arglist(toks, j + 1)
            out.append((toks[i + 1 : j], args))
            i = k
        else:
            i += 1  # this `git` was a different subcommand; keep scanning
    return out


def _overrides_hooks_path(global_opts):
    """True when the global options set `core.hooksPath` for this one invocation, by `-c` or `--config-env`."""
    for i, t in enumerate(global_opts):
        if t in ("-c", "--config-env"):
            value = global_opts[i + 1] if i + 1 < len(global_opts) else ""
        elif t.startswith("--config-env="):
            value = t[len("--config-env=") :]
        else:
            continue
        if value.split("=", 1)[0].lower() == "core.hookspath":
            return True
    return False


# --- Rule 6: a mutating git op against a primary checkout ---------------------------------------------
# `-C <dir>`, `--work-tree <dir>`/`--work-tree=<dir>`, and a `GIT_WORK_TREE=` command-text prefix are the options this rule resolves the mutation target directory from, following real git's own priority rather than a last-one-wins scan across all three.
# `--work-tree`/`GIT_WORK_TREE` name the working tree a mutating command like `reset --hard`/`clean -f` actually writes into, and win regardless of where `-C` points or how many `-C` options preceded it.
# Multiple `-C` options compose sequentially, each resolved against the previous one exactly as git's own "run as if git was started in <path>" describes, an absolute value replacing the running directory outright and a relative one joining onto it.
# `--git-dir`/`GIT_DIR=` alone, with no `--work-tree`/`GIT_WORK_TREE` given anywhere on the same invocation, does not relocate the mutation target at all -- per git's own documented fallback, the working tree stays the effective directory reached by any `-C` chain (or the session's cwd, with none), so this rule never reads `--git-dir`/`GIT_DIR=` as a target-setting option for that purpose.
# An explicit `--git-dir`/`GIT_DIR=` is still read and resolved separately, though, for a different purpose: deciding whether the invocation is primary-checkout or not.
# When `--git-dir` and `--work-tree` are both given and point at different trees, the repository actually mutated is the one `--git-dir` names, not whatever `--work-tree` happens to be -- confirmed live (`git --git-dir=<primary>/.git --work-tree=<empty-dir> commit` mutates `<primary>`, even though `<empty-dir>` resolves as no git repository at all) -- so testing the resolved `--work-tree` value alone for primary-checkout-ness would fail open exactly there.
# Every other value-taking global option is skipped like `_git_subcommand_arglists` already does, since none of the others name a directory this rule reads.
_GIT_ENV_WORK_TREE_VAR = "GIT_WORK_TREE"
_GIT_ENV_GIT_DIR_VAR = "GIT_DIR"


_ENV_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _env_prefix_dirs(toks, git_index):
    """The values of a `GIT_WORK_TREE=`/`GIT_DIR=` assignment immediately preceding the git
    invocation at `toks[git_index]`, in the shape `GIT_WORK_TREE=x GIT_DIR=y git ...`, as a
    `(work_tree, git_dir)` pair, either of which may be `None`. Scans backward only through
    consecutive `NAME=value`-shaped tokens, stopping at the first token that is not one (a shell
    separator, another command, or the start of the string), so this never reads an assignment
    belonging to an earlier, unrelated command in the same compound line. Read from the command's
    own text, not a real process environment: an inline assignment here genuinely does redirect
    the invocation it prefixes, unlike the `GH_WRITE_GUARD_ALLOW=x gh ...` shape documented
    elsewhere in this file, which never reaches the hook's own environment.
    """
    found = {}
    k = git_index - 1
    while k >= 0:
        m = _ENV_ASSIGN_RE.match(toks[k])
        if not m:
            break
        found.setdefault(m.group(1), m.group(2))
        k -= 1
    return found.get(_GIT_ENV_WORK_TREE_VAR), found.get(_GIT_ENV_GIT_DIR_VAR)


_INLINE_ALIAS_RE = re.compile(r"^alias\.(\S+)=(.*)$")


def _git_invocations(cmd):
    """Every `git [global-options] <sub> [args...]` invocation in the command, as
    `(c_dirs, work_tree, git_dir, inline_aliases, sub, args)` tuples. `c_dirs` is the list of
    `-C <dir>` values on this specific invocation, in argv order, since real git composes multiple
    `-C` options sequentially rather than having only the last one take effect. `work_tree` is the
    value of a `--work-tree` global option on this invocation, or a `GIT_WORK_TREE=` prefix
    immediately before it when no `--work-tree` flag is given, or `None` when neither is given, in
    which case the caller resolves the mutation target from the `-C` chain alone. `git_dir` is the
    same shape for `--git-dir`/`GIT_DIR=`: never used to relocate the mutation target on its own
    (matching real git's "no relocation without `--work-tree`" fallback), but read and resolved
    separately, since a `--git-dir` explicitly naming a different tree than `--work-tree` is the
    tree actually mutated, not the one `--work-tree` names. `inline_aliases` is a `name ->
    expansion` dict of every `-c alias.<name>=<value>` override on this invocation, in argv order
    (a later `-c` for the same name wins, matching real git's own repeated `-c` semantics), read
    here since an inline alias is exactly as effective at hiding a mutating command behind an
    unrecognized name as a persisted one, per requirement 6's alias-resolution rule.
    """
    # Shares its tokenizing and global-option skipping with `_git_subcommand_arglists`, generalized to read every subcommand rather than one named subcommand, and to capture the directory-naming and alias-defining options along the way.
    toks = _shell_tokens(cmd)
    n = len(toks)
    out = []
    i = 0
    while i < n:
        if not _is_git_exe(toks[i]):
            i += 1
            continue
        j = i + 1
        c_dirs = []
        work_tree = None
        git_dir = None
        inline_aliases = {}
        while j < n and toks[j].startswith("-"):
            opt = toks[j]
            if opt == "-C":
                if j + 1 < n:
                    c_dirs.append(toks[j + 1])
                    j += 2
                else:
                    j += 1
            elif opt.startswith("--work-tree="):
                work_tree = opt.split("=", 1)[1]
                j += 1
            elif opt == "--work-tree":
                if j + 1 < n:
                    work_tree = toks[j + 1]
                    j += 2
                else:
                    j += 1
            elif opt.startswith("--git-dir="):
                git_dir = opt.split("=", 1)[1]
                j += 1
            elif opt == "--git-dir":
                if j + 1 < n:
                    git_dir = toks[j + 1]
                    j += 2
                else:
                    j += 1
            elif opt == "-c":
                if j + 1 < n:
                    m = _INLINE_ALIAS_RE.match(toks[j + 1])
                    if m:
                        inline_aliases[m.group(1)] = m.group(2)
                    j += 2
                else:
                    j += 1
            elif opt in _GIT_GLOBAL_VALUE_OPTS and "=" not in opt:
                j += 2
            else:
                j += 1
        env_work_tree, env_git_dir = _env_prefix_dirs(toks, i)
        if work_tree is None:
            work_tree = env_work_tree
        if git_dir is None:
            git_dir = env_git_dir
        if j < n and not _is_shell_op(toks[j]):
            sub = toks[j]
            args, k = _collect_arglist(toks, j + 1)
            out.append((c_dirs, work_tree, git_dir, inline_aliases, sub, args))
            i = k
        else:
            i = j if j > i else i + 1  # a bare `git` with no subcommand at all; keep scanning
    return out


def _all_git_invocations(cmd):
    """`_git_invocations` for `cmd` itself, plus for every command string a `sh -c`/`bash -c`-style
    wrapper embeds in it, the same expansion `_all_gh_arg_lists` gives the GitHub-write rules, so a
    mutating git command hidden behind such a wrapper is scanned exactly like a bare one. Each
    tuple carries a seventh element, the leading-`cd` directory in effect for the exact command
    string (outer or inner) it came from -- a `cd` embedded inside a wrapper's own command string
    (`bash -c 'cd /x && git ...'`) is invisible to a leading-`cd` check run only against the outer
    command, and the outer command's own leading `cd` (`cd /x && bash -c 'git ...'`) takes effect
    inside the wrapper too, via the shell's own inherited cwd, when the wrapped string carries no
    leading `cd` of its own to override it. A leading `export GIT_WORK_TREE=x GIT_DIR=y &&` prefix
    is folded into `work_tree`/`git_dir` themselves the same way, whenever the invocation's own
    flags or inline `VAR=x git ...` prefix leave either unset, since an exported assignment
    persists into a following command exactly as effectively as either of those, and this rule
    must not fail open just because the redirect came from `export` rather than from `-C`, an
    inline prefix, or a flag.
    """
    outer_leading_cd = _leading_cd_dir(cmd)
    outer_export_wt, outer_export_gd = _leading_export_dirs(cmd)
    out = []
    for c_dirs, work_tree, git_dir, inline_aliases, sub, args in _git_invocations(cmd):
        work_tree = work_tree if work_tree is not None else outer_export_wt
        git_dir = git_dir if git_dir is not None else outer_export_gd
        out.append((c_dirs, work_tree, git_dir, inline_aliases, sub, args, outer_leading_cd))
    for inner in _embedded_wrapper_commands(cmd):
        leading_cd = _leading_cd_dir(inner) or outer_leading_cd
        inner_export_wt, inner_export_gd = _leading_export_dirs(inner)
        export_wt = inner_export_wt if inner_export_wt is not None else outer_export_wt
        export_gd = inner_export_gd if inner_export_gd is not None else outer_export_gd
        for c_dirs, work_tree, git_dir, inline_aliases, sub, args in _git_invocations(inner):
            work_tree = work_tree if work_tree is not None else export_wt
            git_dir = git_dir if git_dir is not None else export_gd
            out.append((c_dirs, work_tree, git_dir, inline_aliases, sub, args, leading_cd))
    return out


_HOME_VAR_RE = re.compile(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])")


def _expand_dir(value):
    """Expand a leading `~`/`~user` the same way a shell would, plus a literal `$HOME`/`${HOME}`
    reference, both resolvable without executing anything -- `~/repos/<Repo>` is the fleet's own
    documented primary-checkout path convention, so leaving it unexpanded would fail open on the
    single most common way to spell the path this rule exists to catch. The `${HOME}` form is
    always exact, its closing brace delimits the name, but a bare `$HOME` is matched only when not
    immediately followed by another identifier character, so this never matches only a prefix of
    an unrelated variable such as `$HOMEPATH` or `$HOMEDRIVE`. Any other `$VAR` is left as is and
    resolves nowhere real via a plain `-C`, which is this rule's documented fail-open case
    already, not a new one: a hook cannot see a shell variable's runtime value without executing
    something, and it never does.
    """
    if value is None:
        return None
    value = os.path.expanduser(value)
    return _msys_drive_path(_HOME_VAR_RE.sub(lambda _m: os.environ.get("HOME", ""), value))


_MSYS_DRIVE_PATHS = os.name == "nt"
_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(?=/|$)")


def _msys_drive_path(value):
    """Rewrite a leading `/<letter>` drive spelling as `<LETTER>:`, the way MSYS converts an
    argument for a native program, so `/c/repos/x` reads as `C:/repos/x` and a bare `/c` as `C:/`.

    Git Bash hands `git.exe` the converted path, and git refuses the unconverted one. Claude Code
    also reports the hook's `cwd` in that spelling once a Bash `cd /c/...` has run, measured on
    Windows 11. Read raw, either joins to a directory holding no repository, and rule 6 fails open
    there. `classify` converts its `cwd` too, since the origin and push-branch lookups run git in
    it directly. Only on Windows, since `/c/x` on Linux is an ordinary directory. The self-test
    forces the conversion on, and passes no absolute `cwd` beside an absolute target there, since
    Linux reads `C:/x` as relative and would join the two.

    A rooted path with no drive segment, such as `/primary`, is left as is, since MSYS maps it under
    the Git install root, which the guard cannot see without executing something.
    """
    if not _MSYS_DRIVE_PATHS or not value:
        return value
    m = _MSYS_DRIVE_RE.match(value)
    if not m:
        return value
    return f"{m.group(1).upper()}:{value[m.end() :] or '/'}"


def _join_relative(base, value):
    """Expand `~`/`$HOME` in `value`, then join it onto `base` when it is relative and `base` is
    known, or return the expanded value as-is when it is already absolute or there is no base to
    join onto -- the one join rule every directory-naming option (`-C`, a leading `cd`,
    `--work-tree`, `--git-dir`) resolves a relative value with, so a relative spelling always
    resolves against the session's own reported cwd rather than wherever the hook process's own
    OS-level working directory happens to be, which the two are never guaranteed to share.
    """
    v = _expand_dir(value)
    if base and not os.path.isabs(v):
        return os.path.normpath(os.path.join(base, v))
    return v


def _effective_cwd(c_dirs, leading_cd, cwd):
    """The directory git treats as its own current working directory for this invocation, after
    folding a leading `cd` prefix and then any `-C` chain onto the hook's own reported `cwd`, in
    that order -- the same base both `--work-tree` and `--git-dir` resolve a relative value
    against. Multiple `-C` options compose sequentially, each resolved against the previous one
    exactly as git's own "run as if git was started in <path>" describes for a repeated `-C`, an
    absolute value replacing the running directory outright and a relative one joining onto it.
    """
    base = _expand_dir(cwd) if cwd is not None else None
    if leading_cd is not None:
        base = _join_relative(base, leading_cd)
    for c in c_dirs:
        base = _join_relative(base, c)
    return base


def _resolve_target_dir(c_dirs, work_tree, leading_cd, cwd):
    """The mutation target directory for this invocation, following real git's own priority
    rather than a last-option-wins scan across `-C`/`--work-tree`: the effective directory reached
    by a leading `cd` and any `-C` chain (see `_effective_cwd`), with an explicit
    `--work-tree`/`GIT_WORK_TREE=` value, when given, winning over that result regardless of how
    many `-C` options preceded it, matching how `--work-tree`/`GIT_WORK_TREE` name the actual
    mutation target independent of where `-C` points. A relative `work_tree` still resolves
    against the effective directory, the same as git resolves a relative `--work-tree` against its
    own effective directory.
    """
    base = _effective_cwd(c_dirs, leading_cd, cwd)
    if work_tree is not None:
        return _join_relative(base, work_tree)
    return base


def _resolve_repo_dir(git_dir, c_dirs, leading_cd, cwd):
    """The explicit `--git-dir`/`GIT_DIR=` value on this invocation, resolved the same way a
    `--work-tree` value is (joined onto the effective directory reached by a leading `cd` and any
    `-C` chain, see `_effective_cwd`), or `None` when no explicit git-dir was given on this
    invocation at all -- in which case the caller falls back to ordinary ancestor-based repository
    discovery from the resolved mutation target instead, exactly as real git itself does absent an
    explicit `--git-dir`.
    """
    if git_dir is None:
        return None
    return _join_relative(_effective_cwd(c_dirs, leading_cd, cwd), git_dir)


# A single leading `cd <dir> &&`/`cd <dir> ;` prefix, and no more, a narrow, tractable parse rather than tracking shell execution state.
# `git status && cd x && git pull` still resolves the second invocation's target from cwd, a materially smaller gap than an entirely unresolved one.
_CD_CHAIN_SEPS = ("&&", ";")


def _leading_cd_dir(cmd):
    """The directory a command starts with `cd <dir> &&` or `cd <dir> ;`, or `None`."""
    toks = _shell_tokens(cmd)
    if (
        len(toks) >= 3
        and toks[0] == "cd"
        and not toks[1].startswith("-")
        and toks[2] in _CD_CHAIN_SEPS
    ):
        return toks[1]
    return None


def _leading_export_dirs(cmd):
    """The `GIT_WORK_TREE`/`GIT_DIR` values from a single leading `export NAME=value ... &&`/`;`
    prefix, as a `(work_tree, git_dir)` pair, either of which may be `None` -- the same narrow,
    tractable scope `_leading_cd_dir` already takes (only a leading prefix is read, one appearing
    after the first command in a chain is the accepted gap), extended to the one other shell shape
    that redirects a git invocation carrying no `-C`/`--work-tree`/`--git-dir`/inline-prefix of its
    own: a real shell `export` makes an assignment persist into every later command in the same
    session, unlike the inline `VAR=x git ...` prefix `_env_prefix_dirs` already reads, which
    redirects only the one command it immediately precedes. Confirmed live: `export
    GIT_DIR=<primary>/.git GIT_WORK_TREE=<primary> && git reset --hard` discards a tracked local
    modification in `<primary>`, with no redirect at all on the `git` invocation itself, a shape
    `_env_prefix_dirs` alone cannot see. Bails to `(None, None)` on anything but a clean run of
    `NAME=value` tokens between `export` and the first separator, rather than guessing at a
    non-assignment `export` form (`export -p`, `export EXISTING_VAR` with no `=`).
    """
    toks = _shell_tokens(cmd)
    n = len(toks)
    if not toks or toks[0] != "export":
        return None, None
    work_tree = git_dir = None
    i = 1
    while i < n and not _is_shell_op(toks[i]):
        m = _ENV_ASSIGN_RE.match(toks[i])
        if not m:
            return None, None
        if m.group(1) == _GIT_ENV_WORK_TREE_VAR:
            work_tree = m.group(2)
        elif m.group(1) == _GIT_ENV_GIT_DIR_VAR:
            git_dir = m.group(2)
        i += 1
    if i >= n or i == 1 or toks[i] not in _CD_CHAIN_SEPS:
        return None, None
    return work_tree, git_dir


def _is_primary_checkout(target_dir, git_dir=None):
    """`True` when the repository this invocation targets is a primary (non-worktree) git
    checkout, `False` when it is a linked worktree, `None` when no git repository resolves at all
    (the caller fails open on `None`, matching this rule's own precision-over-recall stance).

    The test is a `rev-parse` comparison, not a filesystem-shape guess: `--git-dir` equals
    `--git-common-dir` for a primary checkout and differs for a linked worktree. A `.git`-is-a-
    directory heuristic is deliberately not used instead, since a plain submodule's `.git` is a
    file while it is still a primary working tree that can lose uncommitted work, and that
    heuristic would wrongly exempt it. `--path-format=absolute` must precede the two paths in
    argv, verified silently ineffective (relative paths, no error) in the other order, which would
    misclassify a primary checkout as a worktree the moment a command runs from one of its
    subdirectories.

    When `git_dir` is given (an explicit `--git-dir`/`GIT_DIR=` was resolved on the invocation),
    the check runs against that value directly (`git --git-dir=<git_dir> rev-parse ...`, no `-C`
    at all) rather than against `target_dir` via ordinary ancestor discovery, since `--git-dir`
    names the repository actually mutated independent of where `--work-tree`/cwd point, confirmed
    live: `git --git-dir=<primary>/.git --work-tree=<empty-dir> commit` mutates `<primary>` even
    though `<empty-dir>` resolves as no git repository at all. Testing `target_dir` in that case
    would fail open exactly there.
    """
    if git_dir is not None:
        argv = ["git", f"--git-dir={git_dir}"]
    else:
        argv = ["git", "-C", target_dir or "."]
    argv += ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"]
    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            # These two lines are paths, and a strict decode of one that is not UTF-8 raises into the handler below, which reads it as unresolvable, and unresolvable is the fail-open branch.
            errors="surrogateescape",
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 - a crashed/absent git binary is treated as "unresolvable", the same fail-open outcome a non-zero exit already produces below, not a defect to propagate as a hook-crashing traceback.
        return None
    if r.returncode != 0:
        return None
    lines = r.stdout.strip().splitlines()
    if len(lines) != 2:
        return None
    return lines[0].strip() == lines[1].strip()


# Flags that turn an otherwise-denied `checkout`/`switch` into a real branch-creating or force-discarding operation, the cases git itself does not already refuse on its own.
# `-b`/`-B` are checkout's own create/force-create spellings; `-c`/`-C` are switch's (switch has no `-b`/`-B`, checkout has no `-c`/`-C`), and both pairs mean the same thing to their own subcommand, confirmed live: `git switch -C <existing-branch>` resets that branch to the current HEAD, discarding any commits unique to it, with no dirty-tree warning at all since it is not a working-tree overwrite.
_CHECKOUT_FORCE_FLAGS = {
    "-b",
    "-B",
    "-c",
    "-C",
    "--force",
    "-f",
    "--discard-changes",
    "--orphan",
    "--create",
    "--force-create",
}
# The single-character short forms above, checked against every character of a short-option token, not just an exact-token match: git bundles boolean short flags together (`-qf` is `-q`+`-f`) and attaches a short flag's own value with no space (`-Bname` is `-B name`), and in both shapes the exact-token check below never sees a bare `-f`/`-B`/`-c`/`-C` to match against.
# `-b`/`-B`/`-c`/`-C` are the only checkout/switch short options that take an attached value at all, so this scan cannot mistake an unrelated flag's attached argument for a force flag.
# Neither subcommand has any other flag using these letters, checked directly against each subcommand's own `-h` output, so this scan produces no false positive on either.
_CHECKOUT_FORCE_CHARS = {"b", "B", "c", "C", "f"}
# Flags that make `git clean` an actual deletion rather than the dry-run it defaults to.
_CLEAN_FORCE_FLAGS = {"-f", "--force"}
# `-n`/`--dry-run` always wins over `-f`/`--force`, confirmed live regardless of which order the two are given in or how many times `-f` repeats: `git clean -f -n`, `-n -f`, and `--dry-run -f` all print "Would remove" and delete nothing.
_CLEAN_DRY_RUN_FLAGS = {"-n", "--dry-run"}


def _args_before_double_dash(args):
    """`args` truncated at the first bare `--`, or `args` unchanged when there is none -- every
    argument from `--` onward is an unconditional pathspec to git, never a flag, confirmed live:
    `git clean -f -- -n` deletes a file literally named `-n` rather than behaving as a dry run,
    and `git clean -- -f` (with no real `-f` before the `--`) names a file rather than forcing
    anything. Flag detection must never scan past this boundary.
    """
    if "--" in args:
        return args[: args.index("--")]
    return args


def _has_clean_dry_run_flag(args):
    """Whether `args` carries `-n`/`--dry-run`, bundled into a short-option cluster (`-nfd`) or
    not, the same bundled-cluster scan `_has_checkout_force_flag` already gives checkout/switch's
    own force flags -- a real, confirmed usability gap this rule's `git clean` case would
    otherwise have: `-nfd` denies the exact same harmless dry run `-n` alone does not, purely
    because it also carries an `f` character the force-flag scan below reads on its own.
    """
    for a in _args_before_double_dash(args):
        if a in _CLEAN_DRY_RUN_FLAGS:
            return True
        if a.startswith("--"):
            continue
        if a.startswith("-") and len(a) > 1 and "n" in a[1:]:
            return True
    return False


def _has_checkout_force_flag(args):
    """Whether `args` carries a checkout/switch force flag, as an exact token
    (`--force`/`--orphan`/`--discard-changes`, or a lone `-f`/`-b`/`-B`) or bundled/attached into a
    short-option cluster (`-qf`, `-Bname`, `-qBname`). A long-option token (`--...`) is never
    scanned character-by-character, only matched exactly, since `--discard-changes` legitimately
    contains an `f`.
    """
    for a in args:
        if a in _CHECKOUT_FORCE_FLAGS:
            return True
        if a.startswith("--"):
            continue
        if a.startswith("-") and len(a) > 1 and any(c in _CHECKOUT_FORCE_CHARS for c in a[1:]):
            return True
    return False


# Subcommands denied unconditionally in a primary checkout, no flag or argv shape exempts them.
_ALWAYS_DENY_SUBS = {
    "reset",
    "rebase",
    "cherry-pick",
    "revert",
    "restore",
    "add",
    "commit",
    "rm",
    "mv",
    "apply",
    "am",
    # A push doesn't mutate the local working tree or HEAD the way the rest of this set does, but it publishes whatever is there, and no documented fleet workflow ever pushes from a primary checkout: every push runs from a task's own worktree instead.
    # Rule 4's own branch-rule checks (_check_push_bypass) already run before this rule and can deny a push on their own grounds, so this is an added, independent reason to deny, not a replacement for that check.
    "push",
}


def _resolves_as_ref(target_dir, ref, verify=None):
    """Whether `ref` resolves as a real ref (branch, tag, or commit-ish) in `target_dir`'s
    repository -- the same test git itself uses to decide whether a bare `checkout`/`switch`
    argument names something it safety-checks (a ref switch, refused when it would overwrite a
    local modification) or falls back to treating the argument as a pathspec restore, which
    carries no such safety check at all. `verify`, when given, stands in for the live subprocess
    call so the self-test runs deterministically.
    """
    if verify is not None:
        return verify(target_dir, ref)
    try:
        r = subprocess.run(
            ["git", "-C", target_dir or ".", "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 - a crashed/absent git binary is treated as "does not resolve as a ref", the safer of the two branches this call disambiguates, not a defect to propagate as a hook-crashing traceback.
        return False
    return r.returncode == 0


# A subcommand name this rule does not recognize could be a git alias rather than an unrelated tool invocation this rule has no reason to inspect, so resolution stops once this many aliases have been chased, rather than looping forever on a self-referential or absurdly deep alias chain.
_MAX_ALIAS_DEPTH = 5


def _config_alias(target_dir, name, config_lookup=None):
    """The expansion text of the git alias named `name` in `target_dir`'s own config (merged
    local/global/system, the same precedence `git config --get` itself reads), or `None` when no
    such alias is defined. `config_lookup`, when given, stands in for the live subprocess call so
    the self-test runs deterministically offline.
    """
    if config_lookup is not None:
        return config_lookup(target_dir, name)
    try:
        r = subprocess.run(
            ["git", "-C", target_dir or ".", "config", "--get", f"alias.{name}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 - a crashed/absent git binary is treated the same as "no alias defined", the caller's existing fall-through-to-allow case, not a defect to propagate as a hook-crashing traceback.
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _resolve_alias(sub, args, inline_aliases, target_dir, config_lookup=None):
    """Expand `sub` through a chain of git aliases -- an inline `-c alias.<name>=<value>` override
    on this invocation first, then the target checkout's own persisted config, matching real
    git's own override order -- up to `_MAX_ALIAS_DEPTH` levels, so a subcommand this rule does
    not otherwise recognize is not silently allowed just because it is spelled as a custom alias
    rather than the built-in name it actually expands to. Returns `(sub, args, opaque)`: `opaque`
    is `True` the moment any alias in the chain is a `!`-prefixed shell command rather than a git
    subcommand alias, since that shape hands git an arbitrary shell string this function does not
    and must not execute to interpret -- the caller denies that shape outright against a primary
    checkout rather than either running it or letting it fall through to an ordinary allow, the
    one place this rule departs from its usual fail-open stance, because the alias definition
    itself is concrete, positive evidence of an attempt to run something via git in exactly the
    directory this rule exists to protect.
    """
    seen = set()
    depth = 0
    while depth < _MAX_ALIAS_DEPTH and sub not in seen:
        seen.add(sub)
        expansion = inline_aliases.get(sub)
        if expansion is None:
            expansion = _config_alias(target_dir, sub, config_lookup)
        if expansion is None:
            return sub, args, False
        if expansion.startswith("!"):
            return sub, args, True
        try:
            expanded = shlex.split(expansion)
        except ValueError:
            # Malformed alias text (unbalanced quotes): treat it as unresolvable rather than crashing the hook on a config value neither the agent nor this rule controls.
            return sub, args, False
        if not expanded:
            return sub, args, False
        sub, args = expanded[0], expanded[1:] + args
        depth += 1
    return sub, args, False


def _primary_checkout_verdict(sub, args, target_dir=None, ref_resolver=None):
    """Whether this `(subcommand, args)` pair is a mutating operation requirement 6 denies against
    a primary checkout: `True` (deny), `False` (exempt, explicitly allowed), or `None` (not a
    subcommand this rule concerns itself with, allowed by falling through). `target_dir` and
    `ref_resolver` are used only by the `checkout`/`switch` case, to disambiguate a bare argument
    from a live git call; every other case is pure text/argv classification.
    """
    if sub == "worktree":
        # `add`/`list`/`prune` are always allowed, the documented way to use a primary checkout from an agent session.
        # `remove` is allowed too unless forced: git itself already refuses to remove a worktree carrying uncommitted changes without --force, so only the forced form reproduces the harm this rule exists to catch.
        # `-f` is bundled the same way checkout/switch's own force flags already are: git requires `-f` given twice to remove a locked worktree, and `-ff` satisfies that, confirmed live.
        # Scanned before any `--`, the same cutoff `clean`'s own force scan already applies: confirmed live that `git worktree remove -- -f` reads `-f` as a worktree path argument (erroring since none is literally named that), not a force flag.
        if not args or args[0] != "remove":
            return None
        return any(
            a in ("-f", "--force") or (a.startswith("-") and not a.startswith("--") and "f" in a)
            for a in _args_before_double_dash(args[1:])
        )
    if sub in ("checkout", "switch"):
        # `--` unambiguously means every following argument is a pathspec, not a ref: `checkout -- <path>`/`checkout <ref> -- <path>` restores that path from the index unconditionally, with none of the "would overwrite a local modification" safety check a ref switch gets.
        if "--" in args:
            return True
        if _has_checkout_force_flag(args):
            return True
        # A bare `-` is itself a real, git-recognized ref (the previous branch), not a flag, even though it starts with the same character every flag does.
        positional = [a for a in args if a == "-" or not a.startswith("-")]
        # More than one bare positional with no `--` is the same ambiguous/pathspec-leaning shape (`checkout <ref> <path>`), denied rather than guessed at.
        # Exactly one is the case that needs disambiguating live, below.
        if len(positional) != 1:
            return True
        # A bare `-` is exempt outright rather than live-checked: it is porcelain shorthand for "the previous branch" that only `checkout`/`switch` themselves understand, and `git rev-parse` (what the live check below runs) does not resolve it as a ref at all, which would otherwise misread this exact safe case as a pathspec.
        if positional[0] == "-":
            return False
        # `checkout <ref>`/`switch <ref>`, with any non-force flag also allowed alongside it (already filtered out of `positional` above), is exempt only when `<ref>` actually resolves as a ref.
        # Git's own ref-switch path refuses to overwrite a local modification regardless of a non-force flag like --detach/-q, but its pathspec-restore fallback (what git runs when the argument is not a ref, such as `git checkout .`) carries no such check, so denying it is exactly as safe as denying the `--` form above.
        # This is the one case in this rule that needs a live git call to decide.
        return not _resolves_as_ref(target_dir, positional[0], ref_resolver)
    if sub in ("merge", "pull"):
        # `--ff-only` can never discard a commit or a local change, failing cleanly instead of mutating when a fast-forward is not possible.
        return "--ff-only" not in args
    if sub in _ALWAYS_DENY_SUBS:
        return True
    if sub == "stash":
        # `list`/`show` only read the stash; everything else, bare `stash`/`push`/`save` included, mutates the working tree the same way `pop`/`apply`/`drop` obviously do.
        return not args or args[0] not in ("list", "show")
    if sub == "clean":
        # -n/--dry-run always wins over -f/--force, confirmed live: `-nfd` deletes nothing, so denying it would add no safety while breaking a genuinely harmless, read-only preview of what a later, real `clean -fd` would remove.
        if _has_clean_dry_run_flag(args):
            return False
        # Scanned before any `--`: everything from `--` onward is an unconditional pathspec, confirmed live that `git clean -f -- -n` deletes a file literally named `-n` rather than reading as a dry run, and `git clean -- -f` names a file rather than forcing anything with no real `-f` before the `--`.
        return any(
            a in _CLEAN_FORCE_FLAGS or (a.startswith("-") and not a.startswith("--") and "f" in a)
            for a in _args_before_double_dash(args)
        )
    return None


# Values of GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT read as not granted, the same convention a shell boolean env var commonly uses, so setting it to "0"/"false"/"no" to turn the grant *off* actually does.
# The bare `environ.get(...)` truthiness this replaces read any non-empty string, that one included, as granted.
_FALSY_ENV_VALUES = {"", "0", "false", "no", "off"}


def _check_primary_checkout_mutation(
    cmd, cwd, environ=None, primary_checkout_lookup=None, ref_resolver=None, config_lookup=None
):
    """Rule 6: deny a mutating git operation run directly against a primary (non-worktree)
    checkout. `environ` is a test seam, the same shape rules 3 and 5 already take.
    `primary_checkout_lookup`, when given, stands in for `_is_primary_checkout` so the self-test
    runs deterministically offline instead of resolving a real checkout on the machine running it.
    `ref_resolver` is the same kind of seam for `_resolves_as_ref`, and `config_lookup` the same
    kind of seam for `_config_alias`.

    Fails open (allow) when no git repository resolves at the target at all, matching the
    footgun rules' precision-over-recall stance rather than rule 4's fail-closed one: the harm
    here needs a positively-identified primary checkout to fire on, and a hard fail-closed would
    deny unrelated Bash work in any non-git directory.
    """
    environ = environ if environ is not None else os.environ
    # Read the same way GH_WRITE_GUARD_ALLOW is: from the environment the session was launched with, never a channel the agent itself can set (an inline `VAR=x cmd` prefix or an `export` inside the same call must not satisfy this).
    grant_value = environ.get("GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT", "").strip().lower()
    if grant_value not in _FALSY_ENV_VALUES:
        return "allow", ""
    # Two independent caches, deliberately never merged into one: an identity-dimension key and a file-dimension key can coincide as the same literal string while needing different test methods (--git-dir=X directly versus ordinary -C X discovery), and a single shared cache keyed only by that string would silently reuse one method's answer for the other's lookup.
    identity_cache = {}
    file_cache = {}
    for c_dirs, work_tree, git_dir, inline_aliases, sub, args, leading_cd in _all_git_invocations(
        cmd
    ):
        resolved = _resolve_target_dir(c_dirs, work_tree, leading_cd, cwd)
        repo_git_dir = _resolve_repo_dir(git_dir, c_dirs, leading_cd, cwd)
        # This rule tests two independent dimensions of "does this touch a primary checkout", since git's own --work-tree/--git-dir split lets a single invocation mutate one repository's index/refs/HEAD while writing working-tree files into an entirely different directory.
        # The identity dimension is the repository whose index, refs, and HEAD actually change: the explicit --git-dir/GIT_DIR= value when one was given (independent of where --work-tree/cwd point, confirmed live to diverge from the mutation target when the two are given together and point at different trees), or, absent one, the repository ordinary ancestor search discovers from the effective cwd (the -C/leading-cd chain) -- never from --work-tree, which only ever redirects where working-tree files are read/written, not where the index, refs, or HEAD live.
        # The file dimension is `resolved` itself, the same mutation target already used everywhere else (work-tree when given, else the effective cwd): confirmed live that `git --work-tree=<other-checkout> reset --hard HEAD~1`, with no --git-dir override, run from inside a primary checkout with a staged change, moves the *primary's own* branch pointer back a commit and discards the primary's own staged index entry (the identity dimension), even though the command's working-tree-file side effects (the file dimension) land in `<other-checkout>` instead -- either dimension resolving primary is enough to deny, since either is a real, distinct way this invocation can destroy a primary checkout's own state.
        identity_key = (
            repo_git_dir if repo_git_dir is not None else _effective_cwd(c_dirs, leading_cd, cwd)
        )
        file_key = resolved
        # Whether this even targets a primary checkout is checked before the subcommand/argv verdict, not after.
        # The verdict for `checkout`/`switch` can need its own live git call to disambiguate a ref from a pathspec, and skipping straight past that for the ordinary case (a checkout in a worktree, or targeting no git repository at all) avoids paying for it where the answer would be "allow" regardless.
        if identity_key not in identity_cache:
            if primary_checkout_lookup is not None:
                identity_cache[identity_key] = primary_checkout_lookup(identity_key)
            elif repo_git_dir is not None:
                identity_cache[identity_key] = _is_primary_checkout(
                    identity_key, git_dir=repo_git_dir
                )
            else:
                identity_cache[identity_key] = _is_primary_checkout(identity_key)
        if file_key not in file_cache:
            file_cache[file_key] = (
                primary_checkout_lookup(file_key)
                if primary_checkout_lookup is not None
                else _is_primary_checkout(file_key)
            )
        is_identity_primary = identity_cache[identity_key]
        is_file_primary = file_cache[file_key]
        repo_key = identity_key if is_identity_primary else file_key
        if not is_identity_primary and not is_file_primary:
            continue
        verdict = _primary_checkout_verdict(sub, args, resolved, ref_resolver)
        if verdict is None:
            # `sub` is not one of this rule's own recognized names -- it may be a git alias (inline `-c alias.<name>=...`, or one persisted in the target checkout's own config) expanding to one of them, which is exactly as effective a way to hide a mutating command as spelling it out directly.
            sub, args, opaque = _resolve_alias(
                sub, args, inline_aliases, repo_git_dir or resolved, config_lookup
            )
            if opaque:
                return "deny", (
                    f"This `git {sub}` resolves to a `!`-prefixed shell alias in a primary "
                    f"checkout ({repo_key}), which this rule cannot safely inspect. Denied "
                    "conservatively rather than risking an unreviewed shell command against a "
                    "checkout a mutating git operation there could destroy another task's "
                    "uncommitted work in. Create or use a worktree instead (`git worktree add "
                    "...`), per GOVERNANCE.md 'Repository Boundaries and Write Safety' and the "
                    "repo-worktree skill. If this primary checkout is genuinely the intended "
                    "target, ask the maintainer to set GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT "
                    "before the session starts."
                )
            verdict = _primary_checkout_verdict(sub, args, resolved, ref_resolver)
        if not verdict:
            continue
        return "deny", (
            f"This `git {sub}` runs directly against a primary checkout ({repo_key}), not a "
            "linked worktree. A mutating git operation there can destroy another task's "
            "uncommitted work. Create or use a worktree instead (`git worktree add ...`), per "
            "GOVERNANCE.md 'Repository Boundaries and Write Safety' and the repo-worktree "
            "skill. If this primary checkout is genuinely the intended target, ask the "
            "maintainer to set GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT before the session starts."
        )
    return "allow", ""


def _gh_arg_lists(cmd):
    """Every `gh [args...]` invocation's own argv, from the token after `gh` up to the next shell
    separator, in `cmd` itself, not inside any `sh -c`/`bash -c` wrapper (`_all_gh_arg_lists` covers
    that). Argv-position parsing, the same as `_git_subcommand_arglists` gives git, so a `--repo`/`-R`
    flag, a `repos/<owner>/<repo>` API path, or a GraphQL query field is read only from where a real gh
    argument sits, never from text carried inside an unrelated flag value elsewhere in the command.
    """
    toks = _shell_tokens(cmd)
    n = len(toks)
    out = []
    i = 0
    while i < n:
        if not _is_gh_exe(toks[i]):
            i += 1
            continue
        args, k = _collect_arglist(toks, i + 1)
        out.append(args)
        i = k
    return out


_SHELL_WRAPPER_EXE = ("sh", "bash", "zsh", "ksh", "dash")


def _is_shell_wrapper_exe(tok):
    """True if the token invokes a shell that runs a `-c <string>` argument as a nested command line."""
    base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower().removesuffix(".exe")
    return base in _SHELL_WRAPPER_EXE


def _embedded_wrapper_commands(cmd, _depth=0):
    """Every command string embedded in a `sh -c '...'`/`bash -c "..."`-style wrapper invocation in
    `cmd`, recursively, capped at a few levels of nesting. A `gh`/`git` call wrapped this way forms no
    standalone `gh`/`git` token of its own, so `_gh_arg_lists` and `_git_subcommand_arglists` would
    otherwise miss it entirely, the same bypass `sh -c 'gh issue comment --repo <foreign>/<repo> ...'`
    exercises against a plain token scan.
    """
    if _depth > 4:
        return []
    out = []
    toks = _shell_tokens(cmd)
    n = len(toks)
    i = 0
    while i < n:
        if _is_shell_wrapper_exe(toks[i]):
            args, k = _collect_arglist(toks, i + 1)
            # `-c` may be clustered with other short options (`bash -lc`, `sh -ec`), the command string still the next argv token.
            # A form left out here is a silent bypass of every rule below, the same shape a bare `-c` closes.
            ci = next(
                (
                    x
                    for x, a in enumerate(args)
                    if a.startswith("-") and not a.startswith("--") and a.endswith("c")
                ),
                None,
            )
            if ci is not None and ci + 1 < len(args):
                inner = args[ci + 1]
                out.append(inner)
                out.extend(_embedded_wrapper_commands(inner, _depth + 1))
            i = k
        else:
            i += 1
    return out


def _all_gh_arg_lists(cmd):
    """`_gh_arg_lists` for `cmd` itself, plus for every command string a `sh -c`/`bash -c`-style wrapper
    embeds in it, so a `gh` call hidden behind such a wrapper is scanned exactly like a bare one.
    """
    out = list(_gh_arg_lists(cmd))
    for inner in _embedded_wrapper_commands(cmd):
        out.extend(_gh_arg_lists(inner))
    return out


def _repo_flag_value(tok):
    """The value carried by a `--repo=value`/`-R=value`/`-Rvalue` (attached-short-form) token, or None
    when tok is not one of those. A bare `--repo`/`-R` is handled separately since its value is the next
    token rather than part of this one.
    """
    if tok.startswith("--repo="):
        return tok[len("--repo=") :]
    if tok.startswith("-R="):
        return tok[len("-R=") :]
    if tok.startswith("-R") and len(tok) > 2 and tok[2] != "=":
        return tok[2:]
    return None


def _gh_write_targets(cmd):
    """Every explicit owner/repo target named in an actual `gh` invocation's own argv (including one
    embedded in a `sh -c`/`bash -c` wrapper): a `--repo`/`-R` flag value, or a `repos/<owner>/<repo>` API
    path token. Argv-position parsing, the way `_push_targets` reads a git push target, so a --repo/repos
    mention that is only prose, inside an unrelated --body/--title value or a commit message, is never
    read as one.
    """
    targets = []
    for args in _all_gh_arg_lists(cmd):
        # `-f` alone is value-taking only inside `api`, on `pr create` it is the boolean `--fill`, so treating it as value-consuming there would swallow a real following `--repo` flag whole.
        flags = _GH_API_VALUE_FLAGS if args and args[0] == "api" else _GH_CREATE_TEXT_VALUE_FLAGS
        n = len(args)
        i = 0
        while i < n:
            t = args[i]
            if t in flags and "=" not in t:
                i += 2  # this flag's own value is opaque text, never a repo target
                continue
            if t in _REPO_FLAG_BARE:
                if i + 1 < n:
                    val = args[i + 1]
                    if "/" in val and "<" not in val:
                        o, r = val.split("/", 1)
                        targets.append((o.lower(), r.lower()))
                i += 2
                continue
            val = _repo_flag_value(t)
            if val is not None:
                if "/" in val and "<" not in val:
                    o, r = val.split("/", 1)
                    targets.append((o.lower(), r.lower()))
                i += 1
                continue
            # A full URL (`gh api https://api.github.com/repos/o/r/...` works exactly like the bare path form) is normalized the same way `_gh_api_path` normalizes it, so a URL-wrapped cross-owner target is not missed.
            # The placeholder check runs on the normalized path, not the raw token: a real URL's own query string or fragment (discarded by normalization) can carry a `<` with no bearing on whether the path itself is a real target.
            normalized = _normalize_api_path(t)
            m = _REPOS_PATH_TOKEN.match(normalized)
            if m and "<" not in normalized:
                targets.append((m.group("owner").lower(), m.group("repo").lower()))
            i += 1
    return targets


def _normalize_api_path(raw):
    """A `gh api` endpoint argument reduced to its bare API path, in every accepted spelling.

    `gh api` accepts a full absolute URL in place of a bare path (`gh api https://api.github.com/graphql`
    works exactly like `gh api graphql`); the scheme, host, query string, and fragment are all stripped
    via `urlsplit`, since `gh` drops a `#fragment` before the request reaches the wire regardless of
    whether it was given as part of a URL or appended straight onto a bare endpoint (verified live for
    both), and a raw prefix strip alone leaves it attached, silently defeating an exact `path ==
    "graphql"` comparison.

    A GitHub Enterprise Server host additionally prefixes REST paths with `/api/v3/` and the GraphQL
    endpoint with `/api/graphql`, so both prefixes are reduced to the same bare form `api.github.com`
    uses, after which the rest of this parser treats every host identically.
    """
    path = urlsplit(raw).path.lstrip("/")
    if path == "api/graphql":
        return "graphql"
    if path.startswith("api/v3/"):
        return path[len("api/v3/") :]
    return path


def _gh_api_path(args):
    """The positional API path argument of a `gh api <path> ...` invocation's own argv, normalized via
    `_normalize_api_path`, or None. Skips the invocation's own value-taking flags first (`-X POST`,
    `-f k=v`, ...) so their values are never mistaken for the path positional.
    """
    if not args or args[0] != "api":
        return None
    n = len(args)
    i = 1
    while i < n:
        t = args[i]
        if t in _GH_API_VALUE_FLAGS and "=" not in t:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return _normalize_api_path(t)
    return None


def _gh_field_value(tok):
    """The `name=value` field text carried by one token, in every field-flag spelling `gh` accepts: a
    bare `-f`/`-F`/`--field`/`--raw-field` (the caller reads the next token as the value), the
    equals-attached long form (`--field=name=value`/`--raw-field=name=value`), the equals-attached short
    form (`-f=name=value`/`-F=name=value`), or the fully attached short form (`-fname=value`/
    `-Fname=value`, no separator at all). Returns None for a bare flag, whose value is the next token
    rather than part of this one.
    """
    for pfx in ("--field=", "--raw-field=", "-f=", "-F="):
        if tok.startswith(pfx):
            return tok[len(pfx) :]
    if tok.startswith(("-f", "-F")) and len(tok) > 2 and tok[2] != "=":
        return tok[2:]
    return None


def _gh_graphql_query(args):
    """The GraphQL query text carried by this `gh api graphql` invocation's own `query=...` field
    argument, in whichever field-flag spelling carries it (`_gh_field_value`), or None. Reads only that
    field token's own content rather than searching the whole command for the mutation's name, so a
    --body or PR description merely describing the mutation is not read as one issuing it.
    """
    n = len(args)
    i = 0
    while i < n:
        t = args[i]
        if t in ("-f", "-F", "--field", "--raw-field"):
            if i + 1 < n and args[i + 1].startswith("query="):
                return args[i + 1][len("query=") :]
            i += 2
            continue
        v = _gh_field_value(t)
        if v is not None:
            if v.startswith("query="):
                return v[len("query=") :]
            i += 1
            continue
        i += 1
    return None


def _gh_has_input(args):
    """True when this `gh api` invocation's own argv carries `--input` (bare or equals-attached), gh's
    flag for supplying the request body from a file or stdin.
    """
    return any(t == "--input" or t.startswith("--input=") for t in args)


def _gh_effective_method(args):
    """The effective HTTP method of a `gh api` invocation's own argv: an explicit `-X`/`--method` value
    when present, in every spelling `gh` accepts, else POST when a field flag or `--input` is present
    (`gh`'s own default for a write-shaped call), else GET.
    """
    n = len(args)
    i = 0
    method = None
    has_field = _gh_has_input(args)
    while i < n:
        t = args[i]
        if t in ("-X", "--method"):
            if i + 1 < n:
                method = args[i + 1].upper()
            i += 2
            continue
        if t.startswith("--method="):
            method = t[len("--method=") :].upper()
            i += 1
            continue
        if t.startswith("-X") and len(t) > 2:
            method = t[3:].upper() if t[2] == "=" else t[2:].upper()
            i += 1
            continue
        if t in ("-f", "-F", "--field", "--raw-field") or _gh_field_value(t) is not None:
            has_field = True
        i += 1
    if method:
        return method
    return "POST" if has_field else "GET"


def _push_arg_lists(cmd):
    return _git_subcommand_arglists(cmd, "push")


def _push_targets(cmd, cwd=None, current_branch=None):
    """Parse every push in the command into a list of (op, branch); op is delete | force | update."""
    results = []
    for args in _push_arg_lists(cmd):
        force = delete = push_all = mirror = tags_only = False
        positionals = []
        i = 0
        while i < len(args):
            t = args[i]
            if t in ("--force", "-f") or t.startswith("--force-with-lease"):
                force = True
            elif t in ("--delete", "-d"):
                delete = True
            elif t == "--all":
                push_all = True
            elif t == "--mirror":
                mirror = True
            elif t == "--tags":
                tags_only = (
                    True  # --follow-tags is NOT tags-only: it also pushes the current branch
                )
            elif t in _PUSH_VALUE_FLAGS:
                i += 1  # skip this flag's value
            elif t.startswith("-"):
                pass  # some other flag (e.g. -u, --no-verify)
            else:
                positionals.append(t)
            i += 1
        # The positionals are the remote followed by any refspecs, and a lone positional is the remote, meaning a bare push.
        refspecs = positionals[1:] if len(positionals) >= 2 else []
        branches = []
        for rs in refspecs:
            if rs.startswith("+"):
                force = True
                rs = rs[1:]
            if rs.startswith(":"):
                delete = True  # `:dst` empty-source refspec deletes dst
            dst = rs.split(":", 1)[1] if ":" in rs else rs
            if dst.startswith("refs/heads/"):
                dst = dst[len("refs/heads/") :]
            elif dst.startswith("refs/"):
                continue  # a tag or other non-branch ref
            if dst:
                branches.append(dst)
        if not refspecs and not delete:
            if mirror:
                # --mirror force-updates and prunes every ref: a force against the protected defaults
                # (a non-existent one just returns no rules and is skipped).
                force = True
                branches = list(_PROTECTED_DEFAULT_ORDER)
            elif push_all:
                branches = list(
                    _PROTECTED_DEFAULT_ORDER
                )  # updates every local branch, protected included
            elif tags_only:
                branches = []  # tags only, no branch is updated
            else:
                b = current_branch if current_branch is not None else _current_push_branch(cwd)
                if b:
                    branches = [b]
        op = "delete" if delete else ("force" if force else "update")
        results.extend((op, br) for br in branches)
    return results


def _handoff(cmd):
    return (
        " The agent must not bypass this - if the bypass is genuinely intended, hand the exact command "
        "to the maintainer to run in their terminal. See GOVERNANCE.md 'Repository Boundaries and Write "
        "Safety' and the Branching Model."
    )


def _check_bypass_flags(cmd):
    """Deny the explicit-bypass flags: they are a bypass by definition, no branch query needed."""
    if _GH_ADMIN_MERGE.search(
        _QUOTED_SPAN.sub("", cmd)
    ):  # a flag inside a quoted body is not a real flag
        return "deny", (
            "This uses `gh pr merge --admin`, which merges past required reviews and status checks using "
            "admin power - a bypass of the merge gate. Merge only when the gate is satisfied."
            + _handoff(cmd)
        )
    # The --no-verify flag and `commit -n` skip the git hooks, so they only matter as an actual argument to a git commit or push.
    # Other tools use --no-verify for unrelated things, and shlex keeps a quoted mention out of the argv.
    # The `-n` form is --no-verify only for commit, since `git push -n` is --dry-run.
    commit_lists = _git_subcommand_arglists(cmd, "commit")
    push_lists = _push_arg_lists(cmd)
    commit_bypass = any(("--no-verify" in a) or ("-n" in a) for a in commit_lists)
    push_bypass = any("--no-verify" in a for a in push_lists)
    if commit_bypass or push_bypass:
        return "deny", (
            "This uses --no-verify, which skips the git hooks (signing, lint, and pre-push gates). "
            "Skipping verification is a bypass; run the command without it." + _handoff(cmd)
        )
    if any(
        _overrides_hooks_path(opts)
        for sub in ("commit", "push")
        for opts, _ in _git_subcommand_invocations(cmd, sub)
    ):
        return "deny", (
            "This overrides core.hooksPath for a commit or push, which runs whatever hooks that directory "
            "holds instead of the repository's, and none where it holds none. That is a hook bypass; run "
            "the command without the override." + _handoff(cmd)
        )
    return "allow", ""


def _check_push_bypass(cmd, cwd, origin, current_branch=None, rules_lookup=None):
    """Deny a git push that would only succeed by bypassing an active branch rule."""
    targets = _push_targets(cmd, cwd, current_branch)
    if not targets:
        return (
            "allow",
            "",
        )  # only a quoted mention or a non-push git command: no git/API work needed
    if rules_lookup is None and origin is None:
        origin = _origin_owner_repo(cwd)
    for op, br in targets:
        if rules_lookup is not None:
            rules = rules_lookup(br)
        elif origin is not None:
            rules = _live_branch_rules(origin[0], origin[1], br)
        else:
            rules = None  # no origin to query the rules against
        if rules is None:
            if br in _PROTECTED_DEFAULT:
                reason = (
                    "this checkout's origin repository could not be determined"
                    if origin is None and rules_lookup is None
                    else "its branch rules could not be read (the API may be unreachable)"
                )
                return "deny", (
                    f"Could not verify '{br}', a protected-by-default branch (main/master/develop), "
                    f"because {reason}. Failing closed rather than risk a silent bypass."
                    + _handoff(cmd)
                )
            continue  # an unknown-rules feature/other branch: nothing to bypass, let it through
        if op == "update" and "pull_request" in rules:
            return "deny", (
                f"This is a direct push to '{br}', whose branch rules require a pull request "
                f"(rule: pull_request); it only lands by bypassing that rule with admin power. Use the "
                f"protocol path - commit on a feature branch and open a PR (feature -> squash -> develop, "
                f"or develop -> merge -> main)." + _handoff(cmd)
            )
        if op == "force" and (rules & {"non_fast_forward", "required_linear_history"}):
            return "deny", (
                f"This force-pushes '{br}', whose rules forbid rewriting history "
                f"(rule: non_fast_forward/required_linear_history). Never force-push a protected branch; "
                f"land changes as follow-up commits." + _handoff(cmd)
            )
        if op == "delete" and "deletion" in rules:
            return "deny", (
                f"This deletes '{br}', whose rules forbid deletion (rule: deletion)."
                + _handoff(cmd)
            )
    return "allow", ""


# The GraphQL mutation resolving a review thread, denied when hand-rolled (see `_check_reply_resolve_helper`).
_RESOLVE_THREAD_MUTATION = re.compile(r"\bresolveReviewThread\b")
# The REST endpoint the incident's reply half hand-rolled: `POST /repos/{owner}/{repo}/pulls/{n}/comments/{id}/replies`.
# Distinct from the `addPullRequestReviewThreadReply` GraphQL mutation, which stays allowed as the documented cross-owner fallback (.github/copilot-instructions.md) and is not matched here.
_REPLY_ENDPOINT_PATH = re.compile(r"\bpulls/\d+/comments/\d+/replies\b")


def _check_reply_resolve_helper(cmd, environ):
    """Deny a hand-rolled `resolveReviewThread` mutation or a POST to the review-comment replies
    endpoint, the two-step shape that let a reply sit unresolved across a push and a re-request, reading
    as untriaged to a maintainer skimming the pull request. `scripts/pr_review.py reply ... --resolve`
    captures the thread id from a live query and posts the reply and the resolve as one call, the
    documented path either way.

    Scoped to the query text or API path an actual `gh api graphql`/`gh api` invocation's own argv
    carries, including one embedded in a `sh -c`/`bash -c` wrapper, never a substring search over the
    whole command, so a --body or PR description merely describing the mutation or the endpoint is not
    misread as a real call.

    A REST reply is permitted when its own URL names a target the maintainer has already granted this
    session, since the helper refuses a cross-owner pull request outright and the hand-run form is then
    the documented fallback for that specific repository. A `resolveReviewThread` mutation carries no
    target in its own text (the thread id is opaque), so the same fallback is permitted there whenever
    any grant is active this session, a coarser signal than a REST reply gets, and the residual gap the
    module docstring's "precision over recall" already accepts for this class of rule.

    A GraphQL body supplied via `--input` is denied outright when it has no `-f`/`-F query=...` field to
    read instead (`_gh_graphql_query` returns None), since a `resolveReviewThread` mutation there is
    equally invisible to this parser and there is nothing to distinguish it from the inline case above.
    """
    granted = _granted_targets(environ)
    helper = (
        'Use `scripts/pr_review.py reply <N> --repo <owner>/<repo> --match "<words from the finding>" '
        '--body "<answer>" --resolve` instead, which captures the thread id from a live query and posts '
        "the reply and the resolve as one call. See .github/copilot-instructions.md 'Interacting with "
        "GitHub Copilot PR reviews'."
    )
    for args in _all_gh_arg_lists(cmd):
        path = _gh_api_path(args)
        if path == "graphql":
            # --input checked before trusting any -f/-F query=... value, matching `_is_gh_write`.
            # A harmless decoy query alongside --input has no effect on gh's actual request.
            if _gh_has_input(args):
                if granted:
                    continue
                return "deny", (
                    "This gh api graphql call supplies its body via --input, which cannot be inspected "
                    "for a resolveReviewThread mutation, so it is denied by the same rule as an inline "
                    "one. " + helper
                )
            q = _gh_graphql_query(args)
            if q and _MUTATION.search(q) and _RESOLVE_THREAD_MUTATION.search(q):
                if granted:
                    continue
                return "deny", (
                    "This resolves a review thread directly through `gh api graphql` instead of the "
                    "helper that captures the reply and the resolve in one call, so a reply can be left "
                    "unresolved across a push and a re-request. " + helper
                )
        if path and _REPLY_ENDPOINT_PATH.search(path) and _gh_effective_method(args) == "POST":
            m = _REPOS_PATH_TOKEN.match(path)
            if m:
                target = (m.group("owner").lower(), m.group("repo").lower())
                if target in granted or (target[0], "*") in granted:
                    continue  # this exact target is the maintainer's granted cross-owner exception
            elif granted:
                continue  # path carries no readable owner/repo; fall back to grant presence like the graphql case above
            return "deny", (
                "This posts a review-comment reply directly to the REST replies endpoint instead of the "
                "helper that captures the reply and the resolve in one call. " + helper
            )
    return "allow", ""


# --- Rule 7: an unbounded shell wait ------------------------------------------------------------------
_LOOP_KEYWORDS = ("while", "until")


def _opens_loop(toks, i):
    """True if the token at index i opens a loop this rule judges.

    `while` and `until` always do. `for` does only in its arithmetic form, `for ((;;))`, which can
    run forever exactly as `while true` can. A `for x in <words>` is bounded by that word list.
    """
    if not _opens_command(toks, i):
        return False
    if toks[i] in _LOOP_KEYWORDS:
        return True
    return toks[i] == "for" and i + 1 < len(toks) and toks[i + 1].startswith("((")


# A loop keyword opens a loop only in command position, so the word appearing as an argument value is not one.
# These are the words a command can follow directly, beside the operator tokens `_is_separator` already recognizes.
_COMMAND_POSITION_WORDS = {
    "do",
    "then",
    "else",
    "elif",
    "if",
    "{",
    "!",
    "time",
    # A prefix that runs the command after it, so `do command sleep 30` still sleeps.
    "command",
    "env",
    "exec",
    "nohup",
    "setsid",
    "stdbuf",
    "sudo",
    "nice",
    "ionice",
}

_ARITHMETIC_BOUND = re.compile(r"\(\(.*(?:(?<!<)<(?!<)|(?<!>)>(?!>)).*\)\)")

_TEST_COMPARISONS = frozenset({"-lt", "-le", "-gt", "-ge"})

_TEST_CLOSERS = {"[": "]", "[[": "]]", "test": ""}


class _UnmodeledSyntax(ValueError):
    """Syntax bash reads differently from the POSIX lex the shell tokens come from."""


_HEREDOC_OPERATOR = re.compile(r"(?<!<)<<(?!<)")


def _marked_lex(cmd):
    """Tokenize `cmd` as `_operator_lex` does, pairing each token with whether any of it was quoted.

    It reads quotes and escapes as that POSIX lex does, so an escape cannot shift its tokens.
    Outside quotes a backslash takes the next character literally.
    Inside double quotes it escapes only a double quote or a backslash, and is kept before any other.
    Raises ValueError where the quoting does not parse, as that lex does.
    Raises `_UnmodeledSyntax` where bash reads a character as syntax that lex does not model.
    A backtick outside single quotes opens a substitution, whose quotes nest.
    A `$(` or `${` inside double quotes opens one too, with nested quotes of its own.
    A `$'` opens one string, in which a backslash escapes a single quote.
    A `#` starting a word opens a comment, which hides every character up to the newline.
    An unquoted heredoc fed to a shell is read twice, the first unescaping what the second parses.
    Any `<<` outside a `<<<` raises, quoted or in arithmetic too, since no heredoc then goes unseen.
    """
    if _HEREDOC_OPERATOR.search(cmd):
        raise _UnmodeledSyntax("heredoc")
    out = []
    tok, marked, state, i = "", False, None, 0
    while i < len(cmd):
        ch = cmd[i]
        i += 1
        if state != "'" and ch == "`":
            raise _UnmodeledSyntax("backtick substitution")
        if state == '"' and ch == "$" and cmd[i : i + 1] in ("(", "{"):
            raise _UnmodeledSyntax("nested quoting inside double quotes")
        if state in ("'", '"'):
            if ch == state:
                state = "word"
            elif ch == "\\" and state == '"':
                if i == len(cmd):
                    raise ValueError("No escaped character")
                tok += cmd[i] if cmd[i] in '"\\' else ch + cmd[i]
                i += 1
            else:
                tok += ch
            continue
        if state == "op" and ch in _SHELL_OP_CHARS:
            tok += ch
            continue
        if state is not None and (ch in " \t\r" or state == "op" or ch in _SHELL_OP_CHARS):
            out.append((tok, marked))
            tok, marked, state = "", False, None
        if ch in " \t\r":
            continue
        if ch == "#" and state is None:
            raise _UnmodeledSyntax("comment")
        if ch in _SHELL_OP_CHARS:
            tok, state = ch, "op"
        elif ch == "'" and tok.endswith("$"):
            raise _UnmodeledSyntax("ANSI-C quoting")
        elif ch in "'\"":
            marked, state = True, ch
        elif ch == "\\":
            if i == len(cmd):
                raise ValueError("No escaped character")
            tok, marked, state = tok + cmd[i], True, "word"
            i += 1
        else:
            tok, state = tok + ch, "word"
    if state in ("'", '"'):
        raise ValueError("No closing quotation")
    if state is not None:
        out.append((tok, marked))
    return out


def _quoted_mask(cmd, toks):
    """Per token of `toks`, whether `cmd` spelled it quoted or escaped, or None where that is unknown.

    The shell tokens drop their quoting, so a quoted `";"` reads exactly as a separator does.
    A second lex that marks quoting says which is which, and is trusted only where its tokens are
    the shell tokens exactly, which a command the tokenizer's fallbacks split never gives.
    Where bash reads syntax that lex does not model, the mask is the quote-keeping lex's own.
    """
    try:
        marked = _marked_lex(cmd)
    except _UnmodeledSyntax:
        return _quote_kept_mask(cmd, toks)
    except ValueError:
        return None
    if [t for t, _ in marked] != toks:
        return None
    return [m for _, m in marked]


def _quote_kept_mask(cmd, toks):
    """The mask a quote-keeping lex gives, or None where it does not align with `toks`."""
    raw = _quote_kept_tokens(cmd, toks)
    return None if raw is None else [_is_quote_kept(r) for r in raw]


def _is_quote_kept(raw_tok):
    return any(c in raw_tok for c in "'\"\\")


def _quote_kept_tokens(cmd, toks):
    """The tokens of a quote-keeping lex of `cmd`, or None where they do not align with `toks`.

    Aligning means each quote-keeping token unquotes to its shell token, since that lex reads no escape.
    """
    try:
        raw = _operator_lex(cmd, posix=False)
    except (ValueError, TypeError):
        return None
    if len(raw) != len(toks):
        return None
    for r, t in zip(raw, toks):
        try:
            if (shlex.split(r) if _is_quote_kept(r) else [r]) != [t]:
                return None
        except ValueError:
            return None
    return raw


def _bound_in_condition(cond, quoted=None):
    """True if the loop condition `cond` carries a comparison bound, as a test builtin or as arithmetic.

    The two forms are `[ "$i" -lt 120 ]` and `(( SECONDS < 600 ))`, where a shift compares nothing.
    A comparison operator counts only as an argument of a `[`, `[[`, or `test` invocation.
    Inside `[[ ]]` a `&&` joins two tests and a `|` alternates a pattern, so only `]]` closes it.
    A `$(...)`, `<(...)`, or backtick operand is a command of its own, so its flags compare nothing.
    Its parentheses, and a test's own grouping ones, end no test.
    A substitution glued to a word, `x$(...)`, is a command of its own all the same.
    A separator fused to a parenthesis, `;(` or `);`, ends a `[` or `test` as a bare one does.
    A `case` pattern's `)` inside a substitution closes no parenthesis, so it ends no substitution.
    A quoted token, given by `quoted`, is an operand, so a quoted `;`, `(`, or backtick is no syntax.
    Inside `[[ ]]` a quoted word is a string, never its comparison or its closer.
    Where that is unknown, a backtick with no partner after it is a quoted literal all the same.
    Read anywhere in the condition, `ls -lt` and `grep -le` spelled a bound and waited forever.
    The condition alone is read, so arithmetic in a sleeping body is not mistaken for a guard.
    """
    if _ARITHMETIC_BOUND.search(" ".join(cond)):
        return True

    def literal(j):
        return quoted is not None and quoted[j]

    closer = None
    for k, tok in enumerate(cond):
        name = tok.rsplit("/", 1)[-1]
        if closer is None:
            if (
                name in _TEST_CLOSERS
                and not (k and literal(k - 1))
                and (
                    _opens_command(cond, k)
                    or (k > 0 and cond[k - 1] == "builtin" and _opens_command(cond, k - 1))
                )
            ):
                closer = _TEST_CLOSERS[name]
                depth = group = cases = 0
                tick = False
            continue
        opens, closes = tok.count("("), tok.count(")")
        if literal(k) and (tick or depth):
            pass
        elif literal(k):
            if closer == "]]":
                pass
            elif tok in _TEST_COMPARISONS:
                return True
            elif closer and tok == closer:
                closer = None
        elif "`" in tok:
            if tok.count("`") % 2:
                tick = not tick and any(t.count("`") % 2 for t in cond[k + 1 :])
        elif tick:
            pass
        elif _is_shell_op(tok):
            if depth:
                if not cases:
                    depth = max(0, depth + opens - closes)
                cases = cases if depth else 0
                tail = tok[tok.rfind(")") + 1 :]
                if not depth and closer != "]]" and set(tail) & set(";|&\n"):
                    closer = None
            elif closer != "]]" and set(tok) & set(";|&\n"):
                closer = None
            elif opens and (cond[k - 1].endswith("$") or tok in ("<(", ">(")):
                depth = opens
            else:
                group += opens
                if closes > group:
                    closer = None
                group = max(0, group - closes)
        elif depth:
            if tok == "case" and _opens_command(cond, k):
                cases += 1
            elif tok == "esac" and cases:
                cases -= 1
        elif tok in _TEST_COMPARISONS:
            return True
        elif closer and tok == closer:
            closer = None
    return False


def _names_a_stream(target):
    """True if the redirect target is a device or kernel file rather than a file that ends.

    Enumerating the streams that never end does not close, because each one has other spellings.
    `/proc/self/fd/0` re-opens the same pipe `/dev/stdin` does, `/dev/full` reads like `/dev/zero`,
    and a `.` segment defeats a literal compare of either.
    The category is what the command text decides, so the whole of `/dev` and `/proc` reads as no
    bound, and `/dev/null` is denied with them rather than carved out.
    """
    # POSIX requires exactly two leading slashes to be preserved, so `//dev/zero` survives normpath.
    return posixpath.normpath(re.sub(r"^/+", "/", target)).startswith(("/dev/", "/proc/"))


_RESERVED_WORDS = frozenset(
    {
        "!",
        "case",
        "coproc",
        "do",
        "done",
        "elif",
        "else",
        "esac",
        "fi",
        "for",
        "function",
        "if",
        "in",
        "select",
        "then",
        "time",
        "until",
        "while",
        "{",
        "}",
        "[[",
        "]]",
    }
)


def _redirects_stdin(after_done, quoted=None):
    """True if `after_done` binds descriptor 0 to a source that ends, which a `read` drains.

    Three things have to hold, and reading only the first accepted loops that never end.
    The redirect has to bind descriptor 0, since `2<errors` left the `read` on the pipe.
    It has to name a source rather than duplicate a descriptor, since `<&0` rebinds the pipe to
    itself.
    And the source has to be a file rather than a stream, since `/dev/stdin` is the pipe again and
    `/dev/zero` never reaches EOF.

    The last binding is what counts, not the first to qualify. A shell applies redirections in
    order and each replaces the last, so `< in.txt < /dev/zero` reads the stream.

    The loop's command ends at a reserved word as it does at a separator, so the `< f` in
    `if while read l; do sleep 30; done then echo x < f; fi` binds the `echo`.
    That includes a closing word such as `}`, since a pipe inside the compound it closes can feed
    the loop, so `{ yes | while read l; do sleep 30; done } < f` reads the pipe.
    It ends at a comment too, since bash reads nothing after one, so `done # < f` reads the pipe.
    A word opens a comment when it starts with an unquoted `#`, which `quoted` says per token.
    Where that is unknown, every such word reads as one.
    A comment keeps an earlier binding only where it is surely one, since ending early at a `#`
    that is not skips the later binding that applies.
    It is not sure where the quoting is unknown or after an expansion that can hold a space,
    as in `< ${g:- #x} < /dev/zero`, where a `$(` arrives as a token ending in `$`.
    The caller passes no mask where the command holds a carriage return, which the lex splits
    words at and bash does not, so `log\r#x` is one word rather than a comment.
    A redirect whose target opens a comment has none, so it bounds nothing.
    """

    def comment(j):
        return after_done[j].startswith("#") and not (quoted is not None and quoted[j])

    def sure(j):
        return quoted is not None and not any(
            t.endswith("$") or any(c in t for c in ("${", "$[", "`")) for t in after_done[:j]
        )

    bound = False
    i = 0
    while i < len(after_done):
        tok = after_done[i]
        # Only this loop's own invocation, since a redirect on a later command binds nothing it reads.
        # `yes | while read l; do sleep 30; done; cat < f` is fed by the pipe.
        if _is_separator(tok):
            return bound
        if tok in _RESERVED_WORDS:
            return bound
        if comment(i):
            return bound and sure(i)
        fd = ""
        # A descriptor carries as its own token, so `2>&1 < f` arrives as five.
        # Reading the token before the `<` as a descriptor read the previous redirect's target as one.
        if (
            (tok.isdecimal() or tok.startswith("{"))
            and i + 1 < len(after_done)
            and _is_redir_op(after_done[i + 1])
        ):
            fd = tok
            i += 1
            tok = after_done[i]
        if not _is_redir_op(tok):
            i += 1
            continue
        if i + 1 < len(after_done) and comment(i + 1):
            return False
        target = after_done[i + 1] if i + 1 < len(after_done) else ""
        if _is_shell_op(target):
            target = ""
            i += 1
        else:
            i += 2
        if "<" not in tok:
            continue  # an output redirect leaves descriptor 0 where it was
        # A `{name}<` form names a variable rather than a literal and is never descriptor 0.
        if fd.startswith("{"):
            continue
        if any(unicodedata.decimal(c) for c in fd):
            continue  # a redirect on another descriptor leaves descriptor 0 where it was
        # The last binding wins, since bash applies redirections in order and each replaces the last.
        # Returning on the first let `< in.txt < /dev/zero` vouch for the stream that actually binds.
        bound = bool(target) and not ("&" in tok or _names_a_stream(target))
    return bound


def _reads_its_input(keyword, cond, after_done, quoted=None):
    """True if a `while` loop's condition is a `read`, which ends it when the input is exhausted.

    `while read -r line; do ...; sleep 1; done < file` is bounded by its input rather than by a
    clock, and throttling between iterations is the ordinary reason such a loop sleeps at all.
    Three things are required. A leading `read`, after any `NAME=value` assignments, since a `read`
    later in the condition is an argument to something else. That `read` naming no descriptor of its
    own, since `read -u 3` draws on the one it names and not on the one the redirect bound. And an
    input redirect on the loop itself, because a redirect names a source that ends while a pipe's
    producer is unknown from the command text: `yes | while read line; do sleep 30; done` never
    exhausts its input. A process
    substitution is that same unknown producer behind a redirect, so `done < <(yes)` is no bound
    either. Reading an unknown producer as unbounded costs a false deny on a piped
    `find | while read`, which is the safe direction, and the bound such a loop needs is the
    ordinary one.
    """
    if keyword != "while":
        return False
    # A process substitution wears a redirect's clothes and is the same unknown producer a pipe is:
    # `done < <(yes)` and `done < <(tail -f log)` never exhaust, so neither reads as a bound.
    if any(t.startswith(("<(", ">(")) for t in after_done):
        return False
    if not _redirects_stdin(after_done, quoted):
        return False
    words = [t for t in cond if not _ENV_ASSIGN_RE.match(t)]
    if not words or words[0].rsplit("/", 1)[-1] != "read":
        return False
    # `read -u 3` draws on the descriptor it names, so the redirect on descriptor 0 bounds nothing.
    # The option can be clustered, as `read -ru 3` is, so the letter is looked for rather than the token.
    return not any(t.startswith("-") and not t.startswith("--") and "u" in t for t in words[1:])


# A `timeout` duration argument: bare seconds, or one carrying a GNU suffix.
# A zero duration is excluded, since GNU `timeout` documents 0 as disabling the timeout entirely.
_TIMEOUT_DURATION = re.compile(r"^(?!0+(?:\.0*)?[smhd]?$)\d+(?:\.\d+)?[smhd]?$")

_TIMEOUT_SIGNAL_ZERO_NAME = re.compile(r"^(?:sig)?(?:0+|exit)$", re.IGNORECASE)


def _timeout_signal_number(val):
    """The signal GNU `timeout` reads a digit-string `-s` value as, or None where it reads none.

    A bare number is masked the way GNU `timeout` masks it to accept a shell exit status, 0xFF from
    255 up and 0x7F below, so `128` and `256` are signal 0 as surely as `0` is. A number past the
    range of an int is rejected as no signal at all. The length is checked before `int()`, which
    raises on a digit string past 4300 digits and would crash the hook.
    """
    digits = val.lstrip("0")
    if len(digits) > 10:
        return None
    n = int(digits or "0")
    return n & (0xFF if n >= 0xFF else 0x7F) if n <= 0x7FFFFFFF else None


def _is_timeout_signal_zero(val):
    """True if GNU `timeout` reads the `-s` value as signal 0, which is delivered to no process."""
    if re.fullmatch(r"[0-9]+", val):
        return _timeout_signal_number(val) == 0
    return bool(_TIMEOUT_SIGNAL_ZERO_NAME.match(val))


_TILDE_EXPANSION = re.compile(r"(?:^|[=:])~")


def _may_brace_expand(val):
    """True if a `,` or `..` sits between the first `{` and the last `}` of the word.

    Read across the whole span rather than per pair, since bash expands `{Y={},ti}meout` although
    its comma lies outside the inner `{}`, and a quoted `}` is a token character here too.
    """
    i = val.find("{")
    j = val.rfind("}")
    return 0 <= i < j and ("," in val[i + 1 : j] or ".." in val[i + 1 : j])


def _is_shell_rewritable(val):
    """True if the shell may rewrite the word at run time, so the text cannot say what it names.

    A substitution, a brace expansion, a glob, and a tilde expansion each can, as `$SIG`, `{0..0}`,
    `[0]`, and `~` do, and so can zsh's expansion of a leading `=`, as `=timeout` names a path. The
    rewrite can also split one word into several, so `-k $K` becomes `-k 0 -s 0` where `K` holds
    `0 -s 0`. The test reads the characters rather than the quoting, so a quoted word holding one,
    as `'a*b'` does, is read as rewritable too. A brace counts only where a comma or `..`, the forms
    a brace expansion takes, sits between the word's first `{` and its last `}`, so `{` alone and
    the `{}` of `xargs -I{}` do not, and a tilde counts only where it opens the word or follows a
    `=` or `:`, which holds every place bash expands one and some it does not, as `--chdir=~`. A
    `(` inside a word counts too, since the fallback lexer keeps an extglob group such as
    `/usr/bin/@(timeout)` in one word, with no `)` token for `_run_start` to stop at.
    """
    return (
        val.startswith("=")
        or any(ch in val for ch in "$`[*?(")
        or _may_brace_expand(val)
        or bool(_TILDE_EXPANSION.search(val))
    )


def _timeout_option(tok):
    """Split one `timeout` option token the way getopt reads it, as `(option, value)`.

    `option` is `s` for the signal, `k` for the kill-after, or empty for any other flag. `value` is
    the value the token carries itself, or None where the option takes the next token instead. A
    long option matches by any prefix, as getopt_long does, so `--sig=0` is `--signal=0`, and a
    short cluster such as `-vs0` ends at its first value-taking option, the rest being that value.
    """
    if tok.startswith("--"):
        name, eq, val = tok[2:].partition("=")
        for opt, full in (("s", "signal"), ("k", "kill-after")):
            if name and full.startswith(name):
                return opt, (val if eq else None)
        return "", ""
    for j, ch in enumerate(tok[1:], 1):
        if ch in "ks":
            return ch, (tok[j + 1 :] or None)
    return "", ""


def _is_timeout_exe(tok):
    """True if the token invokes `timeout`, path-qualified or `.exe`-suffixed like `_is_git_exe`."""
    base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    # `gtimeout` is the Homebrew coreutils spelling, and the only GNU timeout a macOS host has.
    return base in ("timeout", "timeout.exe", "gtimeout")


def _is_sleep_exe(tok):
    """True if the token invokes `sleep`, recognized the way `_is_timeout_exe` recognizes timeout."""
    base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    return base in ("sleep", "sleep.exe")


# Commands that name their arguments rather than running them, so a shell after one is text.
# `echo bash -c '<loop>'` runs no shell.
# The set is the exemption rather than the rule, so a name missing from it reads as executing.
# That scans a payload which may not need it, rather than skipping one that does.
_NAMES_ITS_ARGUMENTS = {
    "echo",
    "printf",
    "ls",
    "cat",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "sed",
    "awk",
    "head",
    "tail",
    "wc",
    "diff",
    "man",
    "which",
    "type",
    "basename",
    "dirname",
    "realpath",
    "readlink",
    "file",
    "stat",
    # These name a process rather than starting one, so `pkill sleep` runs no sleep.
    "pkill",
    "pgrep",
    "killall",
    "pidof",
}


def _runs_as_command(toks, w, quoted=None):
    """True if the token at index w is the command its run executes, past any prefix or `timeout`.

    Whether a wrapper runs is a different question from whether it is bounded, and conflating them
    let `timeout 0 bash -c '<loop>'` skip its payload entirely: the zero is no bound, so a
    bound-shaped test refused to look inside a shell that really does run the loop.
    """

    if _opens_command(toks, w):
        return True
    start = w
    while start > 0 and not (
        _is_separator(toks[start - 1]) and not (quoted is not None and quoted[start - 1])
    ):
        start -= 1
    # A leading redirection and its target are not the run's command.
    # Stopping the walk at one made `> log bash -c '<loop>'` read as not executed, and it really leaks.
    while start < w and _is_redir_op(toks[start]):
        start += 2
    if start >= w:
        return True
    # The run's head decides, asked the safe way round.
    # A shell is executed unless its head names arguments rather than running them.
    # Asking whether the head is a known launcher read `flock`, `xargs` and a bare redirection as not executing.
    # Each of those runs the shell after it.
    # A name missing from the set below therefore costs a scan rather than a skipped one.
    head = toks[start].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower().removesuffix(".exe")
    return head not in _NAMES_ITS_ARGUMENTS


def _run_start(toks, w, quoted=None):
    """The index of the first token in the command run holding index w, or None where it is unknown.

    The run reaches back to the previous unquoted shell operator, as `quoted` says per token where
    it is known, an opening `(` or `<(` included. Where that operator holds a redirection, or ends
    in a `)` other than a function definition's, None says the text cannot place the run's start. A
    redirection's target is a word of the run, and the lexer fuses a separator into the redirection
    after it, as `;>` is one token, so neither stopping nor walking on is right for both. A `)`
    closes a group whose output may be a word of the run, a `$(...)` or a `<(...)` as in `timeout -s
    KILL 10 $(true) timeout 800`, or a glob group such as `/usr/bin/@(nice)`. Starting the run after
    it hides an outer `timeout`, and its `(` cannot be found on the tokens, since a `$` fused into a
    word, as `nice$(true)` holds, is no token of its own.
    """
    start = w
    while start > 0:
        k = start - 1
        tok = toks[k]
        if not _is_shell_op(tok) or (quoted is not None and quoted[k]):
            start = k
            continue
        if tok.endswith("("):
            break
        if ">" in tok or "<" in tok or (tok.endswith(")") and not _closes_function_name(toks, k)):
            return None
        break
    return start


_FUNCTION_DEFINITION_OPENERS = {
    "function",
    "{",
    "!",
    "time",
    "if",
    "then",
    "elif",
    "else",
    "while",
    "until",
    "do",
}


def _closes_function_name(toks, k):
    """True if the `)` token at index k ends the `()` of a function definition, as in `f() {`.

    The name has to open the command, or follow a word in `_FUNCTION_DEFINITION_OPENERS` or a
    separator other than a `)`, and hold no `$` and no extglob operator, so the `$()` in
    `timeout -s KILL 10 $() timeout 800` and in `nice $() timeout 800` is none, and so is an `@()`,
    which expands to nothing under bash's `extglob` and `nullglob`. A `)` before the name ends a
    group rather than a command.
    """
    if toks[k] == "()":
        n = k - 1
    elif toks[k] == ")" and k > 0 and toks[k - 1] == "(":
        n = k - 2
    else:
        return False
    if n < 0 or _is_shell_op(toks[n]) or any(ch in toks[n] for ch in "$`@*?+!"):
        return False
    if n == 0 or toks[n - 1] in _FUNCTION_DEFINITION_OPENERS:
        return True
    return _is_separator(toks[n - 1]) and not toks[n - 1].endswith(")")


def _timeout_bounds_wrapper(toks, w, quoted=None):
    """True if a `timeout <duration>` runs the shell wrapper at index w, so its payload is bounded.

    The test is on the command run the wrapper sits in, the tokens `_run_start` places, rather than
    on the tokens immediately before it. That run is bounded when it begins with `timeout` and
    carries a duration, so `timeout -k 30 900 nice bash -c '<loop>'` reads as bounded while `timeout
    5 echo hi && bash -c '<loop>'` does not, the `timeout` there running `echo` in a run of its own.
    Where it cannot place them, the run is no bound.

    A `timeout` sending signal 0, in any spelling GNU `timeout` reads as that signal, is no bound
    unless a `-k` in the duration form, given before or after the `-s`, sends a SIGKILL after the
    signal 0. Signal 0 is delivered to no process, so the `timeout` goes on waiting for a child that
    keeps running.

    A word in the run, before the `timeout` or between it and the wrapper, that the shell may
    rewrite at run time, such as `-${F}0`, `"$SIG"`, `{0..0}`, or `X=$T` after `env`, makes the run
    no bound, whatever follows it. The rewrite may name signal 0, a kill-after, or another
    `timeout`, or split into words that end option parsing early. That is a false deny wherever the
    rewrite yields a bound, as it does in `timeout 900 env PATH=$HOME/bin bash -c '<loop>'` and in
    an assignment prefix such as `X=$T timeout 900 bash -c '<loop>'`, which the shell does not
    split, and wherever quoting keeps the word literal, as it does in `timeout 900 env MSG='a*b'
    bash -c '<loop>'`.

    Where a `timeout`'s command is another `timeout`, past any command prefix, the run is bounded
    only when every outer one sends signal 0, which is inert, with no `-k` given a non-empty value,
    and the innermost one is a bound. Any other such nesting is read as no bound, since an outer
    signal can end the inner `timeout` before its deadline and leave the loop under it running. Past
    the command prefixes and assignments directly after an outer duration, a word naming `timeout`
    anywhere before the wrapper is read as that nesting. These readings are a false deny wherever
    the outer signal would have stopped the loop too, wherever the inner deadline ends the loop
    before any outer signal is sent, as in `timeout -s KILL 1000 timeout 800 bash -c '<loop>'`, and
    wherever a prefix's argument merely names `timeout`, as a path ending in `/timeout` does. A
    launcher building the inner `timeout` from its own arguments, as `env -S 'timeout 800'` does,
    names no `timeout` in a word and is not reached. Behind outer ones that each send signal 0 with
    no `-k`, the nesting is a false deny too wherever a prefix between two of them takes an
    argument, as `nice -n 5` does. Behind any other outer one the nesting is denied anyway, so the
    argument changes nothing.

    A bound is read only here, never for a loop at the same level as the `timeout`. `timeout` takes a
    command, and a `while`/`until` keyword is not one: `timeout 5 while true; do sleep 1; done` is a
    syntax error rather than a bounded loop, so a `timeout` earlier on the line bounds nothing that
    follows it.
    """
    start = _run_start(toks, w, quoted)
    if start is None:
        return False
    run = start
    # A run can open with a keyword or a command prefix, as `if timeout 600 bash -c ...` does.
    while start < w and _is_command_prefix(toks[start]):
        start += 1
    if start >= w or not _is_timeout_exe(toks[start]):
        return False
    # A fork between the `timeout` and the wrapper it runs puts the payload out of its reach.
    # One written inside the payload is caught where that payload is read, on its own terms.
    if _forks_out_of_reach(toks[start:w]):
        return False
    if any(_is_shell_rewritable(t) for t in toks[run:w]):
        return False
    i = start + 1
    signal = kill_after = ""
    while i < w:
        tok = toks[i]
        if tok.startswith("-"):
            opt, val = _timeout_option(tok)
            if val is None:
                i += 1  # the option's value is the next token, never the duration
                val = toks[i] if i < w else ""
            if opt == "s":
                signal = val  # getopt keeps the last one given
            elif opt == "k":
                kill_after = val
            i += 1
            continue
        if not _TIMEOUT_DURATION.match(tok):
            return False
        kills = bool(_TIMEOUT_DURATION.match(kill_after))
        i += 1
        while i < w and _is_command_prefix(toks[i]):
            i += 1
        if not any(_is_timeout_exe(t) for t in toks[i:w]):
            return kills or not _is_timeout_signal_zero(signal)
        if kill_after or not _is_timeout_signal_zero(signal) or not _is_timeout_exe(toks[i]):
            return False
        i += 1
        signal = kill_after = ""
    return False


def _is_command_prefix(tok):
    """True if the token runs the command after it, so what follows is still in command position.

    Path-stripped and assignment-aware for the same reason `_is_sleep_exe` is: `/usr/bin/env sleep`
    and `FOO=1 sleep` are both a sleep, and reading only the bare spellings let each through.
    """
    if _ENV_ASSIGN_RE.match(tok):
        return True
    base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower().removesuffix(".exe")
    return base in _COMMAND_POSITION_WORDS


def _opens_command(toks, i):
    """True if the token at index i sits where a shell reads a command name rather than an argument."""
    if i == 0:
        return True
    prev = toks[i - 1]
    return _is_separator(prev) or _is_command_prefix(prev)


def _sleeps(toks, _depth=0, quoted=None, _budget=None, raw=None):
    """True if `toks` runs a `sleep`, including one inside a `sh -c`/`bash -c` or `eval` payload it
    carries. `quoted` is the quote mask of `toks` and `raw` its quote-keeping tokens, each where
    known, which only the `eval` reading uses.
    `_budget` is what remains of `_EVAL_READ_BUDGET` for the command being judged.
    """
    if _depth > 4:
        return False
    if _budget is None:
        _budget = [_EVAL_READ_BUDGET]
    args_end = 0
    for k, tok in enumerate(toks):
        # `_runs_as_command` rather than `_opens_command`, since a launcher's options sit between it and what it runs.
        # `env -i sleep 30` and `sudo -u ci sleep 30` both sleep.
        # `grep -i sleep f` does not, its run's first command being no launcher.
        if _is_sleep_exe(tok) and _runs_as_command(toks, k):
            return True
        if tok == "eval" and k >= args_end and _budget[0] > 0 and _runs_eval(toks, k, quoted, raw):
            payload, args_end = _eval_payload(toks, k, quoted)
            _budget[0] -= len(payload) + _EVAL_READ_COST
            ptoks = _shell_tokens(payload)
            praw = _quote_kept_tokens(payload, ptoks)
            pmask = None if praw is None else [_is_quote_kept(r) for r in praw]
            if _sleeps(ptoks, _depth + 1, pmask, _budget, praw):
                return True
        if not _is_shell_wrapper_exe(tok):
            continue
        # The payload is read from its own token, since re-joining the token list dropped its quoting.
        # `bash -c 'echo a; sleep 30'` rejoined to a command ending at the `;`, and the sleep was lost.
        args, _ = _collect_arglist(toks, k + 1)
        ci = next(
            (
                x
                for x, a in enumerate(args)
                if a.startswith("-") and not a.startswith("--") and a.endswith("c")
            ),
            None,
        )
        # Recursed rather than scanned, so the payload gets the same command-position test: a
        # `pkill sleep` inside one names a sleep as an argument and does not run one.
        if ci is None or ci + 1 >= len(args):
            continue
        ptoks = _shell_tokens(args[ci + 1])
        praw = _quote_kept_tokens(args[ci + 1], ptoks) if "eval" in ptoks else None
        pmask = None if praw is None else [_is_quote_kept(r) for r in praw]
        if _sleeps(ptoks, _depth + 1, pmask, _budget, praw):
            return True
    return False


def _loop_parts(toks, i, quoted=None):
    """(condition tokens, body tokens, index of the closing `done`) for the loop at index i, or None.

    None means the loop is not closed in this command string, a shape this rule leaves alone rather
    than denies, matching the precision-over-recall stance rules 1-3 take.
    `quoted` is the per-token quote mask, where known.
    A quoted word is no reserved word, and a word after a quoted `;` is an argument.
    So `while true; do echo ";" done; sleep 1; done` closes at its last `done` rather than its first.
    """
    n = len(toks)

    def keyword(j, word):
        if toks[j] != word or not _opens_command(toks, j):
            return False
        return quoted is None or not (quoted[j] or (j > 0 and quoted[j - 1]))

    do_at = next((j for j in range(i + 1, n) if keyword(j, "do")), None)
    if do_at is None:
        return None
    depth = 0
    for j in range(do_at + 1, n):
        if keyword(j, "do"):
            depth += 1
        elif keyword(j, "done"):
            if depth == 0:
                return toks[i + 1 : do_at], toks[do_at + 1 : j], j
            depth -= 1
    return None


def _forks_out_of_reach(toks):
    """True if the command forks work that a `timeout` around it can no longer signal.

    Three shapes are recognized, each read over the whole command rather than tied to one loop. A
    background operator leaves the shell free to exit, so the timeout's own child is gone before it
    fires. `coproc` backgrounds with no operator at all. And `setsid` starts a session of its own,
    which no signal to the timeout's process group reaches.
    Recognized rather than exhaustive: a command can reach a new session through a launcher this
    does not name, and the deny those cost is the one requirement 8 reports after the fact.
    Which `&` backgrounds which compound needs a parse a token scan does not have, and four rounds
    of narrowing that scan each closed the shapes they were shown and left the next one: a statement
    between the loop and its group's closer, a `disown` before a `wait`, a subshell the sequencing
    had already reaped.
    A command that forks anything away is read as bounding nothing. That costs a false deny on
    `<loop> & wait`, which is a bound this rule cannot verify anyway.
    """
    for tok in toks:
        if tok == "coproc":
            return True
        if tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower() in ("setsid", "setsid.exe"):
            return True
        if not _is_separator(tok):
            continue
        # The tokenizer fuses a run of operator characters, so one token can hold a closer and an `&`.
        run = tok.strip()
        i = 0
        while i < len(run):
            # `&&` is a separator rather than a background operator.
            if run[i : i + 2] == "&&":
                i += 2
                continue
            # `|&` is `2>&1 |` and `;&`/`;;&` fall through a `case` arm, so neither backgrounds.
            if run[i] == "&" and (i == 0 or run[i - 1] not in "|;"):
                return True
            i += 1
    return False


_EVAL_READ_BUDGET = 256_000
_EVAL_READ_COST = 1_000

_EVAL_RUNNERS = {
    "do",
    "then",
    "else",
    "elif",
    "if",
    "while",
    "until",
    "{",
    "!",
    "time",
    "coproc",
}

_EVAL_RUNNER_BUILTINS = {"command", "builtin"}

_EVAL_RUNNER_OPTIONS = {
    "time": re.compile(r"^-(?:p|-)$"),
    "command": re.compile(r"^-(?:p+|-)$"),
    "builtin": re.compile(r"^--$"),
}

_EVAL_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[.*\])?\+?=", re.DOTALL)

_NAMED_FD_RE = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\}$")


def _runs_eval(toks, i, quoted=None, raw=None):
    """True if the `eval` at index i of `toks` runs.

    It runs where nothing but runners, assignments, and redirections stand between it and the
    operator that opens its run, since an external launcher such as `timeout` or `nohup` cannot run
    a builtin and exits without running anything. A runner is an unquoted `_EVAL_RUNNERS` word,
    since a quoted one is no reserved word, or `command` or `builtin`, quoted or not, each with the
    options `_EVAL_RUNNER_OPTIONS` allows it, where `command -v eval` only names the eval. An fd
    before a redirection is ASCII digits or a `{name}`. A reserved word is one only ahead of every
    other prefix, so `FOO=1 time eval` runs `/usr/bin/time`, which cannot run the eval. The name
    after `function` or `coproc` is skipped, as in `function f { eval`. A separator fused to a
    redirection opens the run as well, as in `echo x;>f eval`. The walk stops at the first other
    word, so each eval of `echo eval eval ...` costs one step rather than a walk to the start of
    its run. An `eval` that is a redirection's target, as in `>eval`, is a file name and runs
    nothing.
    `raw` is the quote-keeping tokens of `toks`, where known. A word is an assignment only where its
    name and `=` are unquoted, since `'FOO=1' eval` runs a command named `FOO=1`. Where `raw` is
    unknown a quoted one still reads as an assignment, a false deny rather than an unread eval.
    """
    if i > 0 and _is_redir_op(toks[i - 1]) and not (quoted is not None and quoted[i - 1]):
        return False
    k = i - 1
    reserved = False
    while k >= 0:
        t = toks[k]
        q = quoted is not None and quoted[k]
        if not q and _is_shell_op(t):
            if _is_redir_op(t) and not _opens_with_separator(t):
                if reserved:
                    return False
                k -= 1
                continue
            return True
        if not q and t in _EVAL_RUNNERS:
            reserved = True
            k -= 1
            continue
        if (
            reserved
            and k > 0
            and toks[k - 1] in ("function", "coproc")
            and not (quoted is not None and quoted[k - 1])
        ):
            k -= 2
            continue
        j = k
        while j >= 0 and toks[j].startswith("-"):
            j -= 1
        runner = toks[j] if j >= 0 else ""
        is_time = runner == "time" and not (quoted is not None and quoted[j])
        if (
            j < k
            and (is_time or (runner in _EVAL_RUNNER_BUILTINS and not reserved))
            and all(_EVAL_RUNNER_OPTIONS[runner].match(o) for o in toks[j + 1 : k + 1])
        ):
            reserved = is_time
            k = j - 1
            continue
        if reserved:
            return False
        if t in _EVAL_RUNNER_BUILTINS:
            k -= 1
            continue
        if _EVAL_ASSIGNMENT_RE.match(t) and (raw is None or _EVAL_ASSIGNMENT_RE.match(raw[k])):
            k -= 1
            continue
        fd = (t.isascii() and t.isdigit()) or _NAMED_FD_RE.match(t)
        if not q and fd and _is_redir_op(toks[k + 1]):
            k -= 1
            continue
        if k > 0 and _is_redir_op(toks[k - 1]) and not (quoted is not None and quoted[k - 1]):
            k -= 1
            continue
        return False
    return True


def _eval_payload(toks, i, quoted=None):
    """(the shell text the `eval` at index i of `toks` runs, the index its arguments end at).

    The text is what bash builds: the arguments, one leading `--` dropped, joined with spaces.
    A later `eval` before that index is one of the arguments rather than a command at this level, so
    a reader skips it and meets it inside the payload. Reading each one of a chain of them as a
    command, with the rest of the chain as its payload, at every depth, took over a minute for sixty.

    `quoted` is a `_quoted_mask` of `toks`, or None where the quoting is unknown. A quoted separator
    among the arguments is one of them, and becomes a separator only once bash rereads the payload.
    Where the quoting is unknown the payload runs to the end of the eval's line, since any separator
    on it may be a quoted one. A newline token is a real line break, since a quoted newline stays
    inside its word, so no later line is read as the payload. The tokenizer fuses a newline into an
    operator beside it, as a line ending in `;` gives, and that token ends the line as well. The
    index returned there is that of the first separator-shaped token, since one may be real, and
    then a later eval is a command whose own words the joined payload would no longer keep apart,
    so a reader reads that eval itself.
    The tokenizer fuses a separator and a redirection that touch, as in `;>`, and that token ends the
    arguments, since what follows its separator is another command.

    A redirection on the `eval` applies to the whole payload, so the payload is read as a group the
    redirection follows, with its target quoted, and binds no single command inside it.
    """
    n = len(toks)
    known = quoted is not None
    mask = quoted if known else [False] * n
    words, redirs = [], []
    k = i + 1
    while k < n:
        t = toks[k]
        if t == "\n" or (known and not mask[k] and _opens_with_separator(t)):
            break
        if not known and "\n" in t and _is_shell_op(t):
            break
        if not mask[k] and _is_redir_op(t) and not _opens_with_separator(t):
            if words and words[-1].isdigit() and not mask[k - 1]:
                redirs.append(words.pop())
            redirs.append(t)
            if k + 1 < n and (mask[k + 1] or not _is_shell_op(toks[k + 1])):
                redirs.append(shlex.quote(toks[k + 1]))
                k += 1
        else:
            words.append(t)
        k += 1
    if words[:1] == ["--"]:
        words = words[1:]
    end = k
    if not known:
        end = next(
            (
                j
                for j in range(i + 1, k)
                if _is_separator(toks[j]) or _opens_with_separator(toks[j])
            ),
            k,
        )
    if not redirs:
        return " ".join(words), end
    return "{ " + " ".join(words) + "\n} " + " ".join(redirs), end


def _opens_with_separator(tok):
    """True if the operator token `tok` starts with a separator, alone or fused to a redirection.

    `;>`, `|>`, and `&&>` each end the command before the redirection, where `&>` and `>|` are
    redirections whole.
    """
    if not _is_shell_op(tok):
        return False
    return tok[0] in ";|()\n" or tok.startswith("&&") or (tok[0] == "&" and tok[1:2] != ">")


def _unbounded_wait_loop(cmd, inherited_timeout=False, _depth=0, _budget=None):
    """The first unbounded wait loop in `cmd`, as `<keyword> <condition>` text, or None when none.

    A wait loop is a `while`/`until` compound whose body calls `sleep`. It passes when its own
    condition carries an arithmetic guard, or when it sits inside a `sh -c`/`bash -c` payload that a
    `timeout <duration>` runs. A `timeout` never bounds a loop at its own level, since `timeout`
    takes a command and a loop keyword is not one. A nested loop is judged on its own terms, so an
    unbounded inner wait is denied even inside a bounded outer one, which is what it is: unbounded.
    An `eval`'s arguments are a payload the same way, read by `_eval_payload`. They run in this same
    shell, so only a bound on this shell reaches them, `timeout` being unable to run a builtin.
    A payload was unescaped by the outer lex rather than by bash, so it takes the quote-keeping mask.
    `_budget` is what remains of `_EVAL_READ_BUDGET` for the command being judged, across every
    depth and shared with `_sleeps`. Each eval payload read spends its length plus
    `_EVAL_READ_COST`. Where the quoting is unknown each payload runs to the end of its line, so a
    later eval on that line is read both inside it and on its own, and sixty evals on one line took
    most of a minute, as did ten on a long one. An eval past the budget goes unread, which is what
    the guard did with every eval before it read any.
    """
    if _depth > 4:
        return None
    if _budget is None:
        _budget = [_EVAL_READ_BUDGET]
    toks = _shell_tokens(cmd)
    raw = _quote_kept_tokens(cmd, toks) if _depth or "eval" in toks else None
    if _depth:
        mask = None if raw is None else [_is_quote_kept(r) for r in raw]
    else:
        mask = _quoted_mask(cmd, toks)
    forks_away = _forks_out_of_reach(toks)
    args_end = 0
    for i, tok in enumerate(toks):
        # A wrapper is read where its run executes it, covering `timeout 600 bash -c` and `nice bash -c`.
        # Reading one anywhere denied `echo bash -c '...'`, which runs no shell at all.
        if _is_shell_wrapper_exe(tok) and _runs_as_command(toks, i, mask):
            args, _ = _collect_arglist(toks, i + 1)
            # `-c` may be clustered with other short options (`bash -lc`), the command string still the next argv token, the same reading `_embedded_wrapper_commands` gives it.
            ci = next(
                (
                    x
                    for x, a in enumerate(args)
                    if a.startswith("-") and not a.startswith("--") and a.endswith("c")
                ),
                None,
            )
            if ci is not None and ci + 1 < len(args):
                local = _timeout_bounds_wrapper(toks, i, mask)
                # A fork anywhere in this command puts the payload out of an inherited timeout's reach.
                # `timeout 600 bash -c "bash -c '<loop>' &"` outlives the shell that timeout controls.
                backgrounded = forks_away
                bounded = (inherited_timeout and not backgrounded) or local
                inner = _unbounded_wait_loop(args[ci + 1], bounded, _depth + 1, _budget)
                if inner is not None:
                    return inner
        elif tok == "eval" and i >= args_end and _budget[0] > 0 and _runs_eval(toks, i, mask, raw):
            payload, args_end = _eval_payload(toks, i, mask)
            _budget[0] -= len(payload) + _EVAL_READ_COST
            bounded = inherited_timeout and not forks_away
            inner = _unbounded_wait_loop(payload, bounded, _depth + 1, _budget)
            if inner is not None:
                return inner
        elif _opens_loop(toks, i):
            parts = _loop_parts(toks, i, mask)
            if parts is None:
                continue
            cond, body, done_at = parts
            # `while sleep 30; do ...; done` is the standard poll-forever idiom.
            # Its condition sleeps as surely as a body does, and a sleep handed to `sh -c` is still one.
            do_at = i + 1 + len(cond)
            sleeps = _sleeps(
                body,
                quoted=mask[do_at + 1 : done_at] if mask else None,
                _budget=_budget,
                raw=raw[do_at + 1 : done_at] if raw else None,
            ) or _sleeps(
                cond,
                quoted=mask[i + 1 : do_at] if mask else None,
                _budget=_budget,
                raw=raw[i + 1 : do_at] if raw else None,
            )
            # A backgrounded loop is not bounded by a `timeout` around the shell that started it.
            # The shell forks the loop and exits, so `timeout`'s own child is gone and it signals nothing.
            # Measured: the same leak as having written no bound at all.
            backgrounded = forks_away
            bounded = (inherited_timeout and not backgrounded) or _reads_its_input(
                tok,
                cond,
                toks[done_at + 1 :],
                mask[done_at + 1 :] if mask and "\r" not in cmd else None,
            )
            quoted = mask[i + 1 : i + 1 + len(cond)] if mask else None
            if sleeps and not bounded and not _bound_in_condition(cond, quoted):
                # The trailing separator is the `;` before `do`, which is punctuation rather than part of the condition being quoted back.
                quoted = cond[:-1] if cond and _is_separator(cond[-1]) else cond
                return " ".join([tok] + quoted)
    return None


# A heredoc tag: the word after a `<<` redirection, whose body runs to a line holding it alone.
_HEREDOC_TAG = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*$")


def _ends_pipeline(tok):
    """True if the token ends a pipeline. A `|` continues one, every other separator ends it."""
    return _is_separator(tok) and tok != "|"


def _heredoc_openers_in(toks):
    """Every heredoc a line's `toks` can open, in order, each (tag, is_dash_form, feeds_a_shell)."""
    openers = []
    for k, tok in enumerate(toks):
        if tok != "<<" or k + 1 >= len(toks):
            continue
        nxt = toks[k + 1]
        dash = nxt.startswith("-")
        # A bare `-` token is left unread rather than treated as the dash form.
        # `<<- EOF` and `<< - EOF` tokenize identically, and bash reads the second one's delimiter as `-`.
        # Reading both as tag `EOF` strips past a real `-` terminator and drops the commands after it.
        # Not reading either costs a false deny on a document written through the spaced dash form.
        # That is the safe direction of the two.
        tag = nxt[1:] if dash else nxt
        if not _HEREDOC_TAG.fullmatch(tag):
            continue
        # Whether a shell reads this body is a question about the heredoc's whole pipeline.
        # `cat <<EOF | bash` really does hand the body to bash, and so does any prefix before one.
        # A non-pipe separator ends it, so `bash -c '...' && cat > doc.md <<EOF` writes a document.
        # Over-approximating within the pipeline is the safe direction.
        # Keeping a body gets it scanned, where dropping one hides what it holds from every rule below.
        start = k
        while start > 0 and not _ends_pipeline(toks[start - 1]):
            start -= 1
        end = k
        while end < len(toks) and not _ends_pipeline(toks[end]):
            end += 1
        openers.append((tag, dash, any(_is_shell_wrapper_exe(t) for t in toks[start:end])))
    return openers


_ARITH_OPENERS = ("((", "$[")


def _heredoc_forks(toks):
    """(the heredocs a line's `toks` can open in order, whether a `<<` there can be a shift).

    Each heredoc is a (tag, is_dash_form, feeds_a_shell). A line holding `((` or `$[` can hold a
    shift, since `$(( 1 << n ))` tokenizes to a bare `<<` that no token test can tell from a
    redirection. Skipping such a line outright kept a real heredoc's body as commands, and a
    comparison in that body then vouched for a wait loop whose condition carried no bound.
    """
    return _heredoc_openers_in(toks), any(m in t for t in toks for m in _ARITH_OPENERS)


def _heredoc_openers(line):
    """`_heredoc_forks` for `line`, read from tokens rather than from the raw text.

    Tokens keep a `<<` inside a quoted argument the text it is: a commit message explaining the
    `<< EOF` form opened one, and the strip then deleted every command after it. A `<<<` herestring
    is a token of its own and not this.

    The line is read twice, with a `#` read as text and with its comments dropped, and opens a
    heredoc only where both readings find one, taking the second reading's forks. The second keeps
    a `<<` inside a comment from opening one. A reading that does not parse is replaced by the line
    as `_operator_tokens` reads it.
    """
    readings = []
    for comments in (False, True):
        try:
            readings.append(_context_lex(line, comments))
        except (ValueError, RecursionError):
            readings.append(_operator_tokens(line))
    if not _heredoc_openers_in(readings[0]):
        return [], False
    return _heredoc_forks(readings[1])


def _line_heredoc_openers(line):
    """`_heredoc_openers` as it read a line before `_context_lex`, through `_operator_tokens` alone."""
    return _heredoc_forks(_operator_tokens(line))


_HEREDOC_READING_LIMIT = 16


def _heredoc_body_end(lines, start, opener):
    """(the index after the line closing the body `opener` opens at `start`, [that line]).

    Where no line closes the body, this is (the line count, []).

    A plain `<<` ends only on the tag at column zero, and `<<-` also accepts leading tabs. Accepting
    any indentation instead ended the body early on a doc line that merely read as the tag, and the
    rest was scanned as commands.
    """
    tag, dash = opener
    for j in range(start, len(lines)):
        if (lines[j].lstrip("\t") if dash else lines[j]) == tag:
            return j + 1, [lines[j]]  # the terminator line itself is ordinary text again
    return len(lines), []


def _heredoc_bodies_end(lines, start, openers):
    """(the index after the bodies `openers` open at `start`, [the lines kept], whether all closed).

    The bodies are read in order through the last one that is data. A body fed to a shell before
    it is kept whole, since it is the script that shell runs, and one after it is left to be read
    line by line as before. A body no line closes runs to the end, as bash reads it.
    """
    last = max((k for k, (_, _, fed) in enumerate(openers) if not fed), default=-1)
    kept, closed = [], True
    for tag, dash, fed in openers[: last + 1]:
        end, term = _heredoc_body_end(lines, start, (tag, dash))
        kept.extend(lines[start:end] if fed else term)
        closed = closed and bool(term)
        start = end
    return start, kept, closed


def _heredoc_shift_ends(lines, start, openers):
    """Every (end, [lines kept]) a line holding a shift can leave at `start`, reading each of its
    `<<`s alone and each together with every later one whose body closes, in order.

    A reading counts only where its first body closes, it ends on a body that is data, and it removes
    at least one line, since stripping to the end dropped the rest of a quoted payload holding a
    shift, and removing none is the reading that strips nothing. A body fed to a shell within it is
    kept whole. Every subset instead grew the readings as a power of the `<<` count, so a line
    writing five documents passed the reading limit alone.
    """
    found = {}
    for k in range(len(openers)):
        for run in (openers[k : k + 1], openers[k:]):
            i, kept, last = start, [], None
            for n, (tag, dash, fed) in enumerate(run):
                j, term = _heredoc_body_end(lines, i, (tag, dash))
                if not term:
                    if n == 0:
                        break
                    continue
                kept = kept + (lines[i:j] if fed else term)
                i = j
                if not fed:
                    last = (i, kept)
            if last is not None and last[0] - start > len(last[1]):
                found.setdefault((last[0], tuple(last[1])), None)
    return [(j, list(ends)) for j, ends in found]


def _heredoc_readings(cmd, openers=_heredoc_openers):
    """Every text `cmd` can be with its heredoc bodies removed, or None past the reading limit.
    `openers` decides the heredocs each line opens and whether a `<<` there can be a shift.

    A heredoc body is data rather than a command line, so `cat > notes.md <<EOF` writing this rule's
    own forbidden shape into a document is not that shape being run. A body fed to `sh`/`bash` is
    kept, since there it is the script the shell executes. This is precision over recall in the same
    direction the kit takes elsewhere: a wait inside a script file is likewise unseen.

    Bash reads every body a line opens in turn, each through its own delimiter, before the next
    command. A line opening several is read as opening its first alone and, where every body closes,
    as opening each in order through the last that is data. Reading only the first left the others'
    lines as commands, and a heredoc opener among them swallowed the real commands after. Reading
    only every one in order let a quoted `<<` word, or one inside a substitution, strip real commands.
    A line holding a shift is read as opening nothing and every way `_heredoc_shift_ends` reads it.
    A caller denying on any reading then needs no guess at which `<<` is the shift or which body is
    real. Past `_HEREDOC_READING_LIMIT` readings this returns None rather than building them all.
    """
    if "<<" not in cmd:
        return [cmd]
    lines = cmd.split("\n")
    readings = []
    pending = [(0, [])]
    count = 1
    while pending:
        i, kept = pending.pop()
        while i < len(lines):
            kept.append(lines[i])
            i += 1
            opened, shift = openers(lines[i - 1])
            base = list(kept)
            if shift:
                forks = _heredoc_shift_ends(lines, i, opened)
            else:
                j, term, closed = _heredoc_bodies_end(lines, i, opened)
                end, first, _ = _heredoc_bodies_end(lines, i, opened[:1])
                forks = [(j, term)] if closed and (j, term) != (end, first) else []
                i = end
                kept.extend(first)
            for j, term in forks:
                count += 1
                if count > _HEREDOC_READING_LIMIT:
                    return None
                pending.append((j, base + term))
        readings.append("\n".join(kept))
    return readings


_NAMES_A_LOOP = re.compile(r"\b(?:while|until)\b|\bfor\s*\(\(")


def _check_unbounded_wait(cmd):
    """Rule 7: deny a `while`/`until` + `sleep` wait carrying no bound in the command text.

    The command is judged with its heredoc bodies stripped every way `_heredoc_openers` reads them
    and every way `_line_heredoc_openers` does, and a reading holding an unbounded wait denies it, so
    a heredoc the scan misreads never hides a loop the line reading kept. A command naming no `sleep`
    once its line continuations, quotes, backslashes, and `$` signs are removed holds no such wait
    in any reading, since every token is its text with some of those removed, so it is allowed
    unread. Past the reading limit the command is denied unread only when that text also names a
    loop keyword, matched in its case as bash reads one, since a wait needs both.
    """
    text = re.sub(r"\\\r?\n|[\"'\\$]", "", cmd)
    if "sleep" not in text.lower():
        return "allow", ""
    readings = _heredoc_readings(cmd)
    line_readings = _heredoc_readings(cmd, _line_heredoc_openers)
    if readings is None or line_readings is None:
        if not _NAMES_A_LOOP.search(text):
            return "allow", ""
        return "deny", (
            "This command names `sleep` and a loop keyword and holds more lines where `<<` could "
            "open a heredoc or shift inside `(( ))` than the unbounded-wait rule reads every way a "
            'shell might. Split it into shorter commands. See AGENTS.md "Delegation".'
        )
    candidates = dict.fromkeys(readings + line_readings)
    loop = next((found for found in map(_unbounded_wait_loop, candidates) if found), None)
    if loop is None:
        return "allow", ""
    return "deny", (
        f"This is an unbounded shell wait ({loop[:60]}). A `while`/`until` loop whose body sleeps "
        "carries no bound of its own, and the shell outlives the turn, the subagent, and the session "
        "that started it, so a condition that never comes true runs until the machine is rebooted. "
        "Put the bound in the command. A `timeout` runs the shell that runs the loop, as "
        "`timeout <seconds> bash -c '<the loop>'`, since `timeout` takes a command and a loop "
        "keyword is not one. The other accepted bound is "
        "an arithmetic guard in the loop's own condition, such as `(( SECONDS < end ))` or "
        '`[ "$i" -lt 120 ]`. Neither form tells a condition that is failing from one that is merely '
        "unmet, so run the condition once in the foreground first and say so in what the wait "
        "reports. A `break` in the body is not read as a bound here, since only the command text is "
        "judged and a `break` says nothing about when it is reached. Prefer the mechanism that "
        "already signals: a dispatched task reports its own "
        "completion, so polling its output file is a second and unreliable channel for an answer "
        'already on its way. See AGENTS.md "Delegation".'
    )


def classify(
    cmd,
    cwd=None,
    origin=None,
    current_branch=None,
    rules_lookup=None,
    environ=None,
    primary_checkout_lookup=None,
    ref_resolver=None,
    config_lookup=None,
):
    """Return (decision, reason). decision is 'allow' or 'deny'.

    origin, when given, is a (owner, repo) tuple used instead of resolving from cwd - the self-test
    passes it for a deterministic, offline run. current_branch, rules_lookup, environ,
    primary_checkout_lookup, ref_resolver and config_lookup are likewise test seams:
    current_branch stands in for the git resolution of a bare push, rules_lookup(branch) stands in
    for the live branch-rules query, environ stands in for the process environment the
    maintainer's grant is read from, primary_checkout_lookup(dir) stands in for resolving a real
    checkout's primary-vs-worktree status on the machine running the self-test, ref_resolver(dir,
    ref) stands in for the live check that disambiguates a bare `checkout`/`switch` argument as a
    ref rather than a pathspec, and config_lookup(dir, name) stands in for the live git-config
    read that resolves a persisted (non-inline) alias.
    """
    # Fold shell line-continuations so a multi-line Bash invocation, such as `gh pr merge 5 \<newline> --admin`, parses as one command.
    # Only backslash-newline is joined, so a real newline between commands still separates them.
    cmd = re.sub(r"\\\r?\n", " ", cmd)
    cwd = _msys_drive_path(cwd)
    # Rule 4 covers a git operation that would only succeed by bypassing an active branch rule.
    # It is checked before the gh-write gate below, since `git commit --no-verify` is a bypass yet not a GitHub write.
    dec, reason = _check_bypass_flags(cmd)
    if dec == "deny":
        return dec, reason
    # The `_push_targets` helper tokenizes with shlex and keys off a real `git push` argv adjacency, so a push named only inside a quoted argument yields no target.
    # The raw substring is just a cheap pre-filter.
    if _GIT_PUSH.search(cmd):
        dec, reason = _check_push_bypass(cmd, cwd, origin, current_branch, rules_lookup)
        if dec == "deny":
            return dec, reason
    # Rule 6 covers a mutating git operation against a primary checkout, also not a GitHub write, so it is checked here too, before the gh-write gate below would otherwise skip past it.
    dec, reason = _check_primary_checkout_mutation(
        cmd, cwd, environ, primary_checkout_lookup, ref_resolver, config_lookup
    )
    if dec == "deny":
        return dec, reason
    # Rule 7 covers an unbounded shell wait, which is neither a GitHub write nor a git operation, so it is checked here too, before the gh-write gate below would skip past it.
    dec, reason = _check_unbounded_wait(cmd)
    if dec == "deny":
        return dec, reason

    if not _is_gh_write(cmd):
        return "allow", ""

    # 1. Suppressed output on a write.
    #    Quoted argument values are removed before the scan.
    #    That way a --body/--title only mentioning a suppression token as text does not false-deny a legitimate write.
    if _SUPPRESS.search(_QUOTED_SPAN.sub("", cmd)):
        return "deny", (
            "This is a GitHub write with its output discarded or forced to success "
            "(>/dev/null, 2>/dev/null, &>/dev/null, || true, || :, || echo). "
            "A write's result is exactly what must be read: a mutation can succeed on the server "
            "while the client reports an error. Run it without the output-discarding tail and read "
            "the response. See GOVERNANCE.md 'Repository Boundaries and Write Safety'."
        )

    # 2. Literal node id in a mutation
    if _GRAPHQL.search(cmd) and _MUTATION.search(cmd):
        for m in _FIELD_ASSIGN.finditer(cmd):
            val = m.group("v").strip("'\"")
            if val.startswith(("$", "${")):
                continue
            if _NODE_ID_LITERAL.match(val):
                return "deny", (
                    f"This mutation passes a literal GitHub node id ({val[:16]}...) instead of a "
                    "variable captured from a live query. Node ids resolve globally, so a fabricated "
                    "or stale id writes to a real object in another repository. Capture the id from a "
                    'query in this session into a variable and pass -F ...="$VAR". See GOVERNANCE.md '
                    "'Repository Boundaries and Write Safety'."
                )

    # 3. Explicit target outside the origin's owner
    if origin is None:
        origin = _origin_owner_repo(cwd)
    # `_gh_write_targets` reads argv position within each real `gh` invocation, so a compound command carrying one target per invocation still has every one read (the write after `&&` is not skipped).
    # A --repo/repos/<owner>/<repo> mention living inside an unrelated --body/--title/-f value, or in a non-gh command entirely, is not read as a target.
    targets = _gh_write_targets(cmd)
    # This only runs when origin resolves, meaning a git checkout, since with no project context there is nothing to compare an explicit target against, so the check is skipped and rules 1 and 2 still apply.
    # A node-id target is invisible here regardless, which is what rule 2 guards.
    if origin:
        granted = _granted_targets(environ)
        for t in targets:
            if not _target_permitted(t, origin, granted):
                return "deny", (
                    f"This write targets {t[0]}/{t[1]}, under a different owner than this checkout's "
                    f"origin ({origin[0]}/{origin[1]}). Writes reach the origin and its sibling "
                    f"repositories under {origin[0]}, and a different owner is the shape that caused a "
                    f"stray comment on a stranger's repository. Ask the maintainer to grant it in "
                    f'{_ALLOW_ENV} ("{t[0]}/{t[1]}", or "{t[0]}/*" for that whole owner) before the '
                    "session starts, and do not set it yourself, since a permission the agent grants "
                    "itself is not a permission. See GOVERNANCE.md 'Repository Boundaries and Write "
                    "Safety', or the same section of the user-level CLAUDE.md where the repo has no "
                    "GOVERNANCE.md."
                )

    # 5. Hand-rolled reply/resolve for a review thread, bypassing scripts/pr_review.py's one-call helper.
    dec, reason = _check_reply_resolve_helper(cmd, environ)
    if dec == "deny":
        return dec, reason

    return "allow", ""


# --- Self-test ---------------------------------------------------------------------------------------
_CASES = [
    # (command, expected_decision, label)
    (
        'echo "$(echo "\'")"; gh issue comment 1 --repo stranger/x --body hi; echo "\'"  # it\'s',
        "deny",
        "a line whose quoting does not parse and is not comment-stripped still shows the write bash runs",
    ),
    (
        'echo "start\nx # y"; gh issue comment 1 --repo stranger/x --body hi\necho done  # it\'s',
        "deny",
        "a `#` inside a quote an earlier line opened is text, so the write after the quote closes is seen",
    ),
    (
        "(( 1 #x )); gh issue comment 1 --repo stranger/x --body hi  # it's",
        "deny",
        "a `#` inside arithmetic is text, so the write after it is seen beside an apostrophe comment",
    ),
    (
        "[[ $x == @(a|#b) ]]; gh issue comment 1 --repo stranger/x --body hi  # it's",
        "deny",
        "a `#` inside an extglob pattern is text, so the write after it is seen beside an apostrophe comment",
    ),
    (
        "[[ a =~ a|#b ]]; gh issue comment 1 --repo stranger/x --body hi  # it's",
        "deny",
        "a `#` inside a regex is text, so the write after it is seen beside an apostrophe comment",
    ),
    (
        "gh issue comment 1 --body 'fixed; see log' --repo stranger/x  # it's",
        "deny",
        "an operator inside a quoted argument stays in it beside an apostrophe in a trailing comment",
    ),
    (
        "gh issue comment 1 --body 'fixed (see log)' --repo stranger/x  # it's",
        "deny",
        "parentheses inside a quoted argument stay in it beside an apostrophe in a trailing comment",
    ),
    (
        "gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"  # it's",
        "deny",
        "a quoted mutation stays whole beside an apostrophe in a trailing comment",
    ),
    (
        'gh api graphql -f query=\'mutation($t:ID!){addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$t,body:"x"}){comment{id}}}\' -F t="PRRT_kwDODvuuzM6SFvx0" >/dev/null 2>&1 || true',
        "deny",
        "the incident: suppressed + literal id",
    ),
    (
        "gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"PRRT_kwDOabc123def\"",
        "deny",
        "literal node id in a mutation",
    ),
    (
        "gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"",
        "deny",
        "captured $TID still denied: resolve is reserved for the pr_review.py helper (#757)",
    ),
    (
        'gh issue comment 5 -R mankatcheung/job-finder --body "hi"',
        "deny",
        "cross-origin explicit -R",
    ),
    (
        'gh issue comment 5 -R "mankatcheung/job-finder" --body "hi"',
        "deny",
        "cross-origin quoted -R",
    ),
    ("gh pr create --title x --body y >/dev/null 2>&1", "deny", "suppressed gh pr create"),
    (
        'gh api repos/ptr727/PlexCleaner/issues/1/comments -f body="ok"',
        "allow",
        "gh api POST to origin",
    ),
    (
        'gh api graphql -f query=\'{repository(owner:"o",name:"r"){pullRequest(number:1){reviewThreads(first:100){nodes{id}}}}}\'',
        "allow",
        "graphql READ query",
    ),
    ("gh pr view 5 --json reviews", "allow", "gh pr view (read)"),
    ("return 1 2>/dev/null || exit 1", "allow", "shell guard, not a gh write"),
    ("git commit -m 'x' && git push >/dev/null 2>&1", "deny", "push with discarded output"),
    (
        "gh issue comment 5 --body x 2>&1 | tee out.log",
        "allow",
        "bare 2>&1 piped to tee is not suppression",
    ),
    ("gh pr comment 5 --body ok 2>&1", "allow", "bare 2>&1 leaves output visible"),
    (
        "gh api repos/ptr727/PlexCleaner/issues/1/comments -f body=x 2>/dev/null",
        "deny",
        "stderr discarded on a write",
    ),
    (
        'gh issue comment 5 --body "run make || true to skip errors"',
        "allow",
        "|| true inside a quoted body is not a tail",
    ),
    (
        'gh pr comment 5 --body "pipe noisy output to >/dev/null"',
        "allow",
        ">/dev/null inside a quoted body is not a redirect",
    ),
    (
        'gh issue comment 5 --body "see notes" >/dev/null',
        "deny",
        "real redirect after a quoted body still denies",
    ),
    (
        'gh issue comment 5 --body "he said \\"pipe to >/dev/null\\" today"',
        "allow",
        "escaped quotes in a body do not end the span early",
    ),
    ("gh pr close 5 || :", "deny", "force-success no-op tail on a write"),
    ("gh pr comment 5 --body x || echo done", "deny", "force-success echo tail on a write"),
    (
        'gh api graphql -f query=\'mutation{addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$t,body:$b}){comment{id}}}\' -F t="$TID" -F b="fixed_the_underscore_bug_here"',
        "allow",
        "underscored reply body is not a node id",
    ),
    (
        "gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"TODO_fixit\"",
        "deny",
        "not a node id, but still a hand-rolled resolve: denied by rule 5",
    ),
]

# Rule-5 cases, covering the hand-rolled reply/resolve denial and its cross-owner grant escape.
# Each carries the environment the grant is read from, matching the _SCOPE_CASES convention below.
_REPLY_RESOLVE_CASES = [
    # (command, environ, expected_decision, label)
    (
        "gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"",
        {},
        "deny",
        "hand-rolled resolve with no grant",
    ),
    (
        'gh api repos/ptr727/PlexCleaner/pulls/5/comments/9/replies -f body="fixed"',
        {},
        "deny",
        "hand-rolled REST reply with no grant",
    ),
    (
        "gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"",
        {_ALLOW_ENV: "esphome/esphome"},
        "allow",
        "cross-owner grant present: hand-run resolve is the documented fallback",
    ),
    (
        'gh api repos/esphome/esphome/pulls/5/comments/9/replies -f body="fixed"',
        {_ALLOW_ENV: "esphome/esphome"},
        "allow",
        "REST reply permitted only for the exact granted target",
    ),
    (
        'gh api repos/ptr727/PlexCleaner/pulls/5/comments/9/replies -f body="fixed"',
        {_ALLOW_ENV: "esphome/esphome"},
        "deny",
        "an unrelated grant does not exempt a same-owner REST reply (#757 review)",
    ),
    (
        'gh api graphql -f query=\'mutation($t:ID!,$b:String!){addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$t,body:$b}){comment{id}}}\' -F t="$TID" -F b="Fixed in abc123: summary."',
        {},
        "allow",
        "addPullRequestReviewThreadReply mutation is the documented fallback shape, not denied",
    ),
    (
        'gh pr create --title "Guard hand-rolled resolve" --body "Denies a POST to the review-comment replies endpoint and a resolveReviewThread mutation, per #757."',
        {},
        "allow",
        "a --body merely describing the mutation/endpoint is not read as issuing one",
    ),
    (
        'sh -c \'gh api graphql -f query="mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}" -F t="$TID"\'',
        {},
        "deny",
        "a resolve hidden behind sh -c is still caught (#757 review)",
    ),
    (
        'bash -c "gh api repos/ptr727/PlexCleaner/pulls/5/comments/9/replies -f body=fixed"',
        {},
        "deny",
        "a REST reply hidden behind bash -c is still caught (#757 review)",
    ),
    (
        'gh api graphql --field=query=mutation{resolveReviewThread(input:{threadId:$t}){thread{isResolved}}} -F t="$TID"',
        {},
        "deny",
        "the equals-attached --field=query=... spelling is still caught (#757 review)",
    ),
    (
        "gh api graphql -Fquery='mutation{resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"",
        {},
        "deny",
        "the attached-short-form -Fquery=... spelling is still caught (#757 review)",
    ),
    (
        "gh api repos/ptr727/PlexCleaner/pulls/5/comments/9/replies --method GET -f page=1",
        {},
        "allow",
        "a GET to the replies endpoint is a read, not the denied POST (#757 review)",
    ),
]

# Rule-3 cases, covering repository scope.
# Each carries the environment the grant is read from, so the run never depends on the environment the self-test happens to inherit.
# Origin is ptr727/plexcleaner throughout.
_SCOPE_CASES = [
    # (command, environ, expected_decision, label)
    (
        "gh issue create --repo ptr727/PhotoCleaner --title x --body y",
        {},
        "allow",
        "sibling repo under the same owner",
    ),
    (
        "gh api repos/ptr727/PhotoCleaner/issues -f title=x",
        {},
        "allow",
        "sibling repo via an explicit API path",
    ),
    (
        "gh issue create --repo esphome/esphome --title x --body y",
        {},
        "deny",
        "different owner with no grant",
    ),
    (
        "gh issue create --repo esphome/esphome --title x --body y",
        {_ALLOW_ENV: "esphome/esphome"},
        "allow",
        "different owner named in the grant",
    ),
    (
        "gh issue create --repo esphome/esphome --title x --body y",
        {_ALLOW_ENV: "esphome/*"},
        "allow",
        "different owner granted by owner wildcard",
    ),
    (
        "gh issue create --repo esphome/aioesphomeapi --title x",
        {_ALLOW_ENV: "esphome/esphome"},
        "deny",
        "a repo grant does not extend to that owner's other repos",
    ),
    (
        "gh issue comment 5 -R mankatcheung/job-finder --body hi",
        {_ALLOW_ENV: "esphome/*"},
        "deny",
        "the incident: a grant for one owner does not reach another",
    ),
    (
        "gh issue create --repo esphome/esphome --title x",
        {_ALLOW_ENV: "not-an-owner-repo"},
        "deny",
        "a malformed grant grants nothing",
    ),
    (
        "GH_WRITE_GUARD_ALLOW=esphome/esphome gh issue create --repo esphome/esphome --title x",
        {},
        "deny",
        "an inline env prefix is part of the command, not the hook's environment",
    ),
    # Every spelling of the target flag.
    # A form the extraction misses is a silent bypass of rule 3 rather than a near-miss, so each is asserted against a foreign owner that must deny.
    ("gh issue create --repo=esphome/esphome --title x", {}, "deny", "--repo=value equals form"),
    ("gh issue create -R=esphome/esphome --title x", {}, "deny", "-R=value equals form"),
    ("gh issue create -Resphome/esphome --title x", {}, "deny", "-Rvalue attached short form"),
    (
        "gh issue create --repo ptr727/PhotoCleaner --title x && gh issue create --repo esphome/esphome --title y",
        {},
        "deny",
        "a foreign target in the second invocation of a compound is read",
    ),
    (
        "gh issue create --repo=ptr727/PhotoCleaner --title x",
        {},
        "allow",
        "equals form to a sibling owner still allows",
    ),
    (
        'gh issue create --repo ptr727/PhotoCleaner --title "-Resphome/esphome"',
        {},
        "allow",
        "a value opening a quoted span is not a flag",
    ),
    (
        'git commit -m "Without --repo owner/repo, gh run list/view resolve wrong." && git push origin feature/x',
        {},
        "allow",
        "the incident: a commit message quoting --repo owner/repo is not a gh invocation at all",
    ),
    (
        'gh pr comment 5 --body "See the docs on --repo owner/repo and repos/owner/repo usage"',
        {},
        "allow",
        "a --body describing --repo/repos path syntax is opaque text, not a real flag or API path",
    ),
    (
        "sh -c 'gh issue comment 5 --repo esphome/esphome --body hi'",
        {},
        "deny",
        "a cross-owner target hidden behind sh -c is still caught (#757 review)",
    ),
    (
        "bash -lc 'gh api repos/ptr727/PlexCleaner/pulls/5/comments/9/replies -f body=fixed'",
        {},
        "deny",
        "a REST reply behind a clustered bash -lc is still caught (CodeRabbit)",
    ),
    (
        "gh api --hostname github.com repos/ptr727/PlexCleaner/pulls/5/comments/9/replies -f body=fixed",
        {},
        "deny",
        "a value-taking flag before the path does not hide the reply endpoint (CodeRabbit)",
    ),
    (
        "gh api -X POST graphql -f query='mutation{resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"",
        {},
        "deny",
        "-X POST preceding graphql does not hide the mutation (CodeRabbit)",
    ),
    (
        "gh api repos/ptr727/PlexCleaner/pulls/5/comments/9/replies -fbody=fixed",
        {},
        "deny",
        "the attached -fbody=fixed form still enters the write gate (CodeRabbit)",
    ),
    (
        "gh api repos/ptr727/PlexCleaner/pulls/5/comments/9/replies -XPOST",
        {},
        "deny",
        "the attached -XPOST form still enters the write gate (self-found companion to CodeRabbit's -f finding)",
    ),
    (
        "gh api repos/ptr727/PlexCleaner/pulls/5/comments/9/replies -f=body=fixed",
        {},
        "deny",
        "the equals-attached -f=body=fixed form is still caught (CodeRabbit)",
    ),
    (
        "gh api graphql -F=query='mutation{resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"",
        {},
        "deny",
        "the equals-attached -F=query=... form is still caught (CodeRabbit)",
    ),
    (
        "gh api repos/ptr727/PlexCleaner/pulls/5/comments/9/replies -X=GET -f page=1",
        {},
        "allow",
        "the equals-attached -X=GET form is still read as a read (CodeRabbit)",
    ),
    (
        "gh api repos/ptr727/PlexCleaner/pulls/5/comments/9/replies -X=POST",
        {},
        "deny",
        "the equals-attached -X=POST form still enters the write gate (CodeRabbit)",
    ),
    (
        "gh api repos/ptr727/PlexCleaner/pulls/5/comments/9/replies --input body.json",
        {},
        "deny",
        "--input on the replies endpoint is still read as a write (promotion review)",
    ),
    (
        "gh api graphql --method POST --input resolve.json",
        {},
        "deny",
        "a GraphQL body from --input is denied as uninspectable (promotion review)",
    ),
    (
        "gh api graphql --method POST --input resolve.json",
        {_ALLOW_ENV: "esphome/esphome"},
        "allow",
        "an uninspectable --input GraphQL body is permitted under a cross-owner grant, like the inline case",
    ),
    (
        "gh api graphql --input mutation.json -f query='{viewer{login}}'",
        {},
        "deny",
        "a decoy -f query=... alongside --input does not hide an uninspectable body (CodeRabbit)",
    ),
    (
        "gh api https://api.github.com/graphql -f query='mutation{resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"",
        {},
        "deny",
        "a full-URL graphql endpoint still resolves to the resolve mutation (CodeRabbit)",
    ),
    (
        "gh api 'https://api.github.com/graphql#x' -f query='mutation{resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"",
        {},
        "deny",
        "a quoted URL fragment does not hide the resolve mutation (qodo)",
    ),
    (
        "gh api 'graphql#x' -f query='mutation{resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"",
        {},
        "deny",
        "a fragment appended straight onto the bare graphql endpoint is caught too, matching gh's own live behavior",
    ),
    (
        "gh api https://github.example.com/api/graphql -f query='mutation{resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"",
        {},
        "deny",
        "a GitHub Enterprise Server /api/graphql endpoint still resolves to the resolve mutation (CodeRabbit)",
    ),
]

_SCOPE_CASES_MORE: list[tuple[str, dict[str, str], str, str]] = [
    # More Rule-3 scope cases, covering the promotion-review round's findings, kept as their own literal rather than growing the one above further.
    # (command, environ, expected_decision, label)
    (
        "gh api repos/esphome/esphome/issues --jq '.[] | \"-XPOST\"'",
        {},
        "allow",
        "a --jq expression only containing the text -XPOST is a read, not misread as a write (promotion review)",
    ),
    (
        "gh.exe api repos/esphome/esphome/issues -f body=x",
        {},
        "deny",
        "gh.exe is still recognized for an api write (CodeRabbit)",
    ),
    (
        "gh.exe pr create --repo esphome/esphome --title x",
        {},
        "deny",
        "gh.exe is still recognized for a pr-create write (companion to CodeRabbit's gh.exe finding)",
    ),
    (
        "gh api /repos/esphome/esphome/issues -f title=x",
        {},
        "deny",
        "a leading slash on the REST path does not hide the cross-owner target (CodeRabbit)",
    ),
    (
        "gh pr create -f --repo esphome/esphome --title x",
        {},
        "deny",
        "-f as pr create's boolean --fill does not swallow the following --repo (CodeRabbit)",
    ),
    (
        "gh pr create -f --title x --body y",
        {},
        "allow",
        "-f as pr create's boolean --fill does not swallow --title either, with no foreign target present",
    ),
    (
        "gh pr create -F repos/esphome/esphome --title x",
        {},
        "allow",
        "-F stays value-taking (--body-file) on pr create, so a body-file path is not misread as an API target (qodo)",
    ),
    (
        "gh api https://api.github.com/repos/esphome/esphome/issues -f title=x",
        {},
        "deny",
        "a full-URL REST path still resolves to the foreign-owner target (CodeRabbit)",
    ),
    (
        "gh api https://github.example.com/api/v3/repos/esphome/esphome/issues -f title=x",
        {},
        "deny",
        "a GitHub Enterprise Server /api/v3/ REST prefix still resolves to the foreign-owner target (CodeRabbit)",
    ),
    (
        "gh api 'https://api.github.com/repos/esphome/esphome/issues?x=<x>' -f title=y",
        {},
        "deny",
        "a stray < in the query string, discarded by normalization, does not hide the real target (CodeRabbit)",
    ),
    (
        "gh api 'https://api.github.com/repos/esphome/esphome/issues#<x>' -f title=y",
        {},
        "deny",
        "a stray < in the fragment, discarded by normalization, does not hide the real target (CodeRabbit)",
    ),
]

# Rule-4 cases, covering branch-rule bypass.
# Each carries its own branch-to-rules map so the run is deterministic and offline, where the real hook queries the live rules and here rules_lookup is injected.
# The current_branch value stands in for the git resolution of a bare push.
# A `None` rules value means the query could not be read.
_CODE_RULES = {
    "deletion",
    "non_fast_forward",
    "required_linear_history",
    "required_signatures",
    "pull_request",
    "required_status_checks",
    "copilot_code_review",
}  # code-style develop / any main
_CONFIG_RULES = {
    "deletion",
    "non_fast_forward",
    "required_signatures",
}  # config-style develop: no pull_request
_GIT_CASES = [
    # (command, current_branch, {branch: rules_set_or_None}, expected_decision, label)
    (
        "git push origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "code-style develop: direct push bypasses pull_request",
    ),
    (
        "git push origin develop",
        None,
        {"develop": _CONFIG_RULES},
        "allow",
        "config-style develop: no pull_request rule, direct push allowed",
    ),
    (
        "git push origin main",
        None,
        {"main": _CODE_RULES},
        "deny",
        "main: direct push bypasses pull_request",
    ),
    (
        "git push origin feature/x",
        None,
        {"feature/x": set()},
        "allow",
        "feature branch: no rules, allowed",
    ),
    (
        "git push -u origin feature/x",
        None,
        {"feature/x": set()},
        "allow",
        "feature branch with -u: allowed",
    ),
    (
        "git push origin HEAD:develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "HEAD:develop refspec resolves to develop",
    ),
    (
        "git push origin abc1234:refs/heads/main",
        None,
        {"main": _CODE_RULES},
        "deny",
        "sha:refs/heads/main resolves to main",
    ),
    ("git push", "develop", {"develop": _CODE_RULES}, "deny", "bare push resolving to develop"),
    (
        "git push",
        "feature/x",
        {"feature/x": set()},
        "allow",
        "bare push resolving to a feature branch",
    ),
    (
        "git push --force origin develop",
        None,
        {"develop": _CONFIG_RULES},
        "deny",
        "force-push denied by non_fast_forward even on config develop",
    ),
    (
        "git push --force-with-lease origin feature/x",
        None,
        {"feature/x": set()},
        "allow",
        "force-with-lease to a ruleless feature branch",
    ),
    (
        "git push origin +HEAD:develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "+refspec is a force-push to develop",
    ),
    (
        "git push --delete origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "delete develop denied by deletion rule",
    ),
    (
        "git push origin :develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "empty-source :develop is a delete",
    ),
    (
        "git push origin develop",
        None,
        {"develop": None},
        "deny",
        "develop with unreadable rules: fail closed",
    ),
    (
        "git push origin feature/x",
        None,
        {"feature/x": None},
        "allow",
        "feature branch with unreadable rules: fail open",
    ),
    ("git commit --no-verify -m x", None, {}, "deny", "commit --no-verify skips the hooks"),
    ("git commit -n -m x", None, {}, "deny", "commit -n is --no-verify"),
    (
        "git -C /repo commit -n -m x",
        None,
        {},
        "deny",
        "global option before commit -n does not dodge the check",
    ),
    (
        "git -c user.name=x commit --no-verify -m y",
        None,
        {},
        "deny",
        "global option before commit --no-verify does not dodge the check",
    ),
    (
        "git push --no-verify origin feature/x",
        None,
        {"feature/x": set()},
        "deny",
        "push --no-verify is a bypass even on a feature branch",
    ),
    (
        "git -c core.hooksPath=/dev/null commit -m x",
        None,
        {},
        "deny",
        "a per-invocation core.hooksPath on commit is a hook bypass",
    ),
    (
        "git -C /repo -c core.hookspath=.none push origin feature/x",
        None,
        {"feature/x": set()},
        "deny",
        "a per-invocation core.hooksPath on push is a hook bypass, key matched case-insensitively",
    ),
    (
        "git --config-env=core.hooksPath=HOOKS commit -m x",
        None,
        {},
        "deny",
        "--config-env naming core.hooksPath is the same override",
    ),
    (
        "git --config-env core.hooksPath=HOOKS commit -m x",
        None,
        {},
        "deny",
        "--config-env with a separate value is the same override",
    ),
    (
        "git -c core.editor=true commit -m x",
        None,
        {},
        "allow",
        "a -c override of another key is not a hook bypass",
    ),
    (
        "git -c core.hooksPath=.githooks config --list",
        None,
        {},
        "allow",
        "a core.hooksPath override on a command that runs no commit or push hook is not denied here",
    ),
    (
        "git commit -m 'git -c core.hooksPath=x commit'",
        None,
        {},
        "allow",
        "a hooksPath override inside a quoted message is not an option",
    ),
    (
        "git push -n origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "push -n is dry-run not no-verify, but the direct push to develop still denies",
    ),
    (
        "gh pr merge 5 --admin --squash",
        None,
        {},
        "deny",
        "gh pr merge --admin overrides the merge gate",
    ),
    (
        "gh pr merge 5 \\\n  --admin --squash",
        None,
        {},
        "deny",
        "line-continued gh pr merge --admin still caught",
    ),
    (
        "gh.exe pr merge 5 --admin --squash",
        None,
        {},
        "deny",
        "gh.exe pr merge --admin still caught (CodeRabbit)",
    ),
    (
        "git commit -m 'mention --no-verify in the message'",
        None,
        {},
        "allow",
        "--no-verify inside a quoted message is not a flag",
    ),
    (
        "npm publish --no-verify",
        None,
        {},
        "allow",
        "--no-verify on a non-git command is not a git-hook bypass",
    ),
    (
        'gh issue comment 5 --body "run: git push origin develop"',
        None,
        {"develop": _CODE_RULES},
        "allow",
        "a git push mentioned inside a quoted body is not an executed push",
    ),
    (
        "git push origin 'HEAD:develop'",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "a quoted refspec is unquoted by shlex and still resolves to develop",
    ),
    (
        "git -C /repo push origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "global option -C <dir> before push does not dodge Rule 4",
    ),
    (
        "git -c user.name=x push origin main",
        None,
        {"main": _CODE_RULES},
        "deny",
        "global option -c k=v before push does not dodge Rule 4",
    ),
    (
        "git --git-dir=/r/.git push origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "global option --git-dir=... before push does not dodge Rule 4",
    ),
    (
        "git -C /repo push origin feature/x",
        None,
        {"feature/x": set()},
        "allow",
        "global options before push to a feature branch: allowed",
    ),
    (
        "git push --all origin",
        None,
        {"main": _CODE_RULES, "master": set(), "develop": _CODE_RULES},
        "deny",
        "--all updates every branch: a protected default denies",
    ),
    (
        "git push --all origin",
        None,
        {"main": set(), "master": set(), "develop": set()},
        "allow",
        "--all where no default branch is protected: allowed",
    ),
    (
        "git push --mirror origin",
        None,
        {"main": _CODE_RULES, "master": set(), "develop": _CODE_RULES},
        "deny",
        "--mirror force-prunes every ref: a protected default denies",
    ),
    ("git push --tags origin", None, {}, "allow", "--tags pushes tags only, no branch target"),
    (
        "git push --follow-tags origin",
        "develop",
        {"develop": _CODE_RULES},
        "deny",
        "--follow-tags also pushes the current branch: resolves develop",
    ),
    (
        "git push --push-option='a>b' origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "a > inside a quoted option value is not a redirection: develop still parsed",
    ),
    (
        "git push origin feature/x && git push origin develop",
        None,
        {"feature/x": set(), "develop": _CODE_RULES},
        "deny",
        "second push in a compound is checked: develop denies",
    ),
    (
        "git push origin develop && git push origin feature/x",
        None,
        {"develop": _CODE_RULES, "feature/x": set()},
        "deny",
        "first push in a compound is checked: develop denies",
    ),
    (
        "git push origin develop | cat",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "a pipe ends the push argv: develop still parsed",
    ),
    (
        "git push 2>push.log origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "a leading fd redirection is skipped, not a positional: develop still parsed",
    ),
    (
        "git push >log origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "a leading stdout redirection before args does not hide develop",
    ),
    (
        "/usr/bin/git push origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "an absolute-path git is still git: direct push denies",
    ),
    ("/usr/bin/git commit -n -m x", None, {}, "deny", "absolute-path git commit -n is --no-verify"),
    (
        "git.exe push origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "git.exe is still git: direct push denies",
    ),
    (
        'gh issue comment 5 --body "first git push" && git push origin develop',
        None,
        {"develop": _CODE_RULES},
        "deny",
        "a quoted mention before a real push does not hide the real target",
    ),
    (
        "git push >push.log 2>&1",
        "develop",
        {"develop": _CODE_RULES},
        "deny",
        "redirection tokens are not a branch: bare push to develop still denies",
    ),
    (
        "git push origin develop >push.log 2>&1",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "redirect after a real refspec does not hide the develop target",
    ),
    # A newline ends a command as `&&` does, and reading it as whitespace made every token on a later line an argument of the push.
    # A feature-branch push followed by a `gh pr create` then denied as a direct push to the base branch that command named.
    (
        "git push -u origin feature/x\ngh pr create --base develop --title x --body y",
        None,
        {"feature/x": set(), "develop": _CODE_RULES},
        "allow",
        "a newline ends the push argv: the pr-create base is not a push target",
    ),
    (
        "cd /repo\ngit push origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "a push on a later line is still parsed as a push",
    ),
    (
        "git push origin feature/x\ngit push origin develop",
        None,
        {"feature/x": set(), "develop": _CODE_RULES},
        "deny",
        "a second push on the next line is checked: develop denies",
    ),
    (
        "git push \\\n  origin develop",
        None,
        {"develop": _CODE_RULES},
        "deny",
        "a backslash-newline is a continuation, not a separator: develop still parsed",
    ),
    (
        'gh issue comment 5 --body "one line\ngit push origin develop"',
        None,
        {"develop": _CODE_RULES},
        "allow",
        "a newline inside a quoted body does not start a new command",
    ),
    # Unbalanced quoting is what actually reaches the degraded path, and the separator has to survive there too.
    (
        "git push origin feature/x\ngit push origin develop 'unclosed",
        None,
        {"feature/x": set(), "develop": _CODE_RULES},
        "deny",
        "the degraded path keeps the newline: a push on the next line is still read",
    ),
]

# (command, cwd, {dir: is_primary_or_None}, {(dir, ref): resolves_as_ref} or None, expected_decision, label) -- is_primary is True (a primary checkout), False (a linked worktree), or None (unresolved, for example when no repository exists or the git query itself fails, such as on a pre-2.31 git lacking `rev-parse --path-format`).
# A None ref-map means every ref-check in the case resolves True (an ordinary branch name), the common case; only the pathspec-disambiguation cases below need a real map.
_PRIMARY_CHECKOUT_CASES = [
    (
        "git reset --hard origin/main",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "a git reset --hard in a primary checkout is exactly the GOVERNANCE.md-named harm",
    ),
    (
        "git reset --hard origin/main",
        "/worktree",
        {"/worktree": False},
        None,
        "allow",
        "the same command in a linked worktree is allowed",
    ),
    (
        "git checkout main",
        "/primary",
        {"/primary": True},
        {("/primary", "main"): True},
        "allow",
        (
            "a flagless checkout of a real ref is exempt even in a primary checkout, an accepted "
            "scope boundary: the #1073 incident's own literal commands (checkout, then --ff-only "
            "pull) are this exact shape, and the concurrent-access hazard they still carried is "
            "the prose rule's job, not this mechanically decidable one's"
        ),
    ),
    (
        "git checkout .",
        "/primary",
        {"/primary": True},
        {("/primary", "."): False},
        "deny",
        (
            "a single bare argument that does not resolve as a ref falls back to git's own "
            "pathspec-restore path, which carries none of the ref-switch safety check the "
            "flagless exemption relies on"
        ),
    ),
    (
        "git checkout -- .",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "-- unambiguously means every following argument is a pathspec, denied with no ref check",
    ),
    (
        "git checkout HEAD -- src/",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "a ref plus -- pathspec is still the unconditional pathspec-restore form",
    ),
    (
        "git checkout main src/",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "two positional arguments with no -- is the same ambiguous/pathspec-leaning shape, denied rather than guessed at",
    ),
    (
        "git switch feature/x",
        "/primary",
        {"/primary": True},
        {("/primary", "feature/x"): True},
        "allow",
        "switch gets the same ref-verified flagless exemption as checkout",
    ),
    (
        "git switch -C other",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        (
            "switch -C force-resets an existing branch to the current HEAD with no dirty-tree "
            "warning at all, empirically confirmed; -c/-C are switch's own create/force-create "
            "spellings, distinct from checkout's -b/-B, and neither subcommand has any other flag "
            "using those letters"
        ),
    ),
    (
        "git switch -c newbranch",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "switch -c creates a new branch, the same branch-creating class as checkout -b",
    ),
    (
        "git pull",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "a bare pull in a primary checkout is denied",
    ),
    (
        "git pull --ff-only",
        "/primary",
        {"/primary": True},
        None,
        "allow",
        "--ff-only can never discard anything, exempt even in a primary checkout",
    ),
    (
        "git merge --ff-only origin/develop",
        "/primary",
        {"/primary": True},
        None,
        "allow",
        "the documented base-clone cleanup step (repo-worktree 'Listing and Cleanup')",
    ),
    (
        "git worktree add ../x origin/develop",
        "/primary",
        {"/primary": True},
        None,
        "allow",
        "creating a worktree from the primary checkout is the documented, intended use",
    ),
    (
        "git fetch origin develop",
        "/primary",
        {"/primary": True},
        None,
        "allow",
        "a read is never denied, even in a primary checkout",
    ),
    (
        "git status",
        "/primary",
        {"/primary": True},
        None,
        "allow",
        "status is not even a subcommand this rule inspects",
    ),
    (
        "git commit -m x",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "commit has no flag-based exemption, denied unconditionally in a primary checkout",
    ),
    (
        "git add -A",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "a blanket add in a primary checkout is exactly the #1073 incident class",
    ),
    (
        "git rm -rf .",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "git rm deletes working-tree files exactly as unconditionally as add/commit mutate them",
    ),
    (
        "git mv a.txt b.txt",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "git mv renames a tracked file and stages the change exactly as unconditionally as rm deletes one",
    ),
    (
        "git apply patch.diff",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "git apply mutates the working tree",
    ),
    (
        "git am 0001-fix.patch",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "git am mutates the working tree",
    ),
    (
        "git push origin feature/x",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        (
            "a push does not mutate the local working tree the way the rest of this rule's ops "
            "do, but no documented fleet workflow ever pushes from a primary checkout, so it is "
            "denied unconditionally there too, independent of rule 4's own branch-rule checks"
        ),
    ),
    (
        "git push origin feature/x",
        "/worktree",
        {"/worktree": False},
        None,
        "allow",
        "the same push from a linked worktree, where every documented push actually happens, is allowed",
    ),
    (
        "git stash",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "bare stash mutates the working tree exactly as push/pop/apply/drop do",
    ),
    (
        "git stash push -m wip",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "stash push is the same mutation as bare stash, spelled out",
    ),
    (
        "git stash list",
        "/primary",
        {"/primary": True},
        None,
        "allow",
        "stash list only reads the stash, denying it would add no safety",
    ),
    (
        "git clean -fd",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "an ordinary forced clean deletes untracked files/directories, the harm this rule guards against",
    ),
    (
        "git clean -nfd",
        "/primary",
        {"/primary": True},
        None,
        "allow",
        (
            "-n/--dry-run always wins over -f/--force, confirmed live regardless of order: "
            "-nfd deletes nothing, only previews what a later -fd would remove, so denying it "
            "would add no safety while breaking a genuinely harmless preview"
        ),
    ),
    (
        "git clean --dry-run --force",
        "/primary",
        {"/primary": True},
        None,
        "allow",
        "the long-flag spelling of the same dry-run-wins-over-force exemption",
    ),
    (
        "git clean -f -- -n",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        (
            "-n after -- is an unconditional pathspec (a file literally named -n), not the "
            "--dry-run flag, confirmed live: this deletes that file despite the -n-shaped token, "
            "so scanning for a dry-run flag anywhere in args rather than only before -- would "
            "have wrongly exempted a real, forced deletion"
        ),
    ),
    (
        "git clean -- -f",
        "/primary",
        {"/primary": True},
        None,
        "allow",
        (
            "-f after -- is a pathspec (a file literally named -f), not a real force flag, so "
            "with no actual -f/--force before --, git itself refuses to run at all"
        ),
    ),
    (
        "git checkout -b feature/x",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "checkout -b is branch-creating, not the exempt ref-switch form",
    ),
    (
        "git checkout --detach main",
        "/primary",
        {"/primary": True},
        {("/primary", "main"): True},
        "allow",
        (
            "a non-force flag alongside a real ref stays exempt too: --detach changes nothing "
            "about git's own overwrite-refusal on a dirty tracked file, empirically confirmed, so "
            "the exemption is a real-ref-with-no-force-flag test, not a strictly zero-flags one"
        ),
    ),
    (
        "git checkout -qf other",
        "/primary",
        {"/primary": True},
        {("/primary", "other"): True},
        "deny",
        (
            "-qf bundles -q (quiet) and -f (force) into one short-option cluster; an exact-token "
            "check never sees a bare -f to match, but real git still forces the checkout through, "
            "empirically confirmed to discard a dirty tracked file"
        ),
    ),
    (
        "git checkout -Bnewbranch",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        (
            "-Bnewbranch attaches -B's mandatory branch-name value with no space, the same "
            "force-creating operation as -B newbranch as two tokens, empirically confirmed to work"
        ),
    ),
    (
        "git checkout -qt main",
        "/primary",
        {"/primary": True},
        {("/primary", "main"): True},
        "allow",
        "a bundled cluster with no b/B/f character (-q quiet, -t track) is not a force flag",
    ),
    (
        "git worktree remove --force ../x",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        "a forced worktree remove reproduces the harm this rule guards against",
    ),
    (
        "git worktree remove -ff ../x",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        (
            "-ff bundles force twice, exactly the -f -f git itself requires to remove a locked "
            "worktree, confirmed live to forcibly remove one with uncommitted content, which an "
            "exact-token check alone misses since remove has no other short option -f could "
            "combine with"
        ),
    ),
    (
        "git worktree remove -- -f",
        "/primary",
        {"/primary": True},
        None,
        "allow",
        (
            "-f after -- is a worktree path argument, not a force flag, confirmed live: git "
            "reads it as a literal worktree name (erroring since none is named that) rather than "
            "forcing anything, the same -- cutoff clean's own force scan already applies"
        ),
    ),
    (
        "git worktree remove ../x",
        "/primary",
        {"/primary": True},
        None,
        "allow",
        "an unforced worktree remove is exempt: git itself refuses one carrying local changes",
    ),
    (
        "git -C /worktree reset --hard origin/main",
        "/primary",
        {"/primary": True, "/worktree": False},
        None,
        "allow",
        "-C overrides cwd: a worktree named explicitly is allowed even though cwd is the primary",
    ),
    (
        "git -C /primary reset --hard origin/main",
        "/worktree",
        {"/primary": True, "/worktree": False},
        None,
        "deny",
        "-C overrides cwd the other way: the primary named explicitly is denied from a worktree",
    ),
    (
        "git --work-tree /primary reset --hard",
        "/worktree",
        {"/primary": True, "/worktree": False},
        None,
        "deny",
        "--work-tree redirects the mutation the same way -C does, and is read the same way",
    ),
    (
        "GIT_WORK_TREE=/primary GIT_DIR=/primary/.git git reset --hard",
        "/worktree",
        {"/primary/.git": True, "/worktree": False},
        None,
        "deny",
        (
            "a GIT_WORK_TREE=/GIT_DIR= prefix in the command's own text redirects the "
            "invocation, unlike a real env var the hook process never sees; the primary-checkout "
            "test itself keys on the resolved GIT_DIR= value, since that is what --git-dir/GIT_DIR "
            "name for repository identity"
        ),
    ),
    (
        "export GIT_DIR=/primary/.git GIT_WORK_TREE=/primary && git reset --hard",
        "/somewhere-else",
        {"/primary/.git": True, "/somewhere-else": False},
        None,
        "deny",
        (
            "a leading export makes the assignment persist into the following command exactly "
            "as a real shell would, confirmed live to discard a tracked local modification with "
            "no redirect at all on the git invocation itself -- a shape an inline VAR=x git ... "
            "prefix scan alone cannot see, since export and the git invocation are separate "
            "commands joined by &&, not one command with a prefix"
        ),
    ),
    (
        "git status && export GIT_DIR=/primary/.git && git reset --hard",
        "/somewhere-else",
        {"/primary/.git": True, "/somewhere-else": False},
        None,
        "allow",
        (
            "only a leading export is read, matching the same accepted-gap scope leading cd "
            "already has: export here is not the first token of the command (git status is), so "
            "it has no effect under this rule's narrow scope even though a real shell would still "
            "apply it to the following reset --hard"
        ),
    ),
    (
        "git --git-dir=/primary/.git --work-tree=/primary -C /worktree reset --hard",
        "/somewhere-else",
        {"/primary/.git": True, "/worktree": False},
        None,
        "deny",
        (
            "--work-tree wins over -C regardless of argv order for the mutation target message, "
            "and the primary-checkout test itself keys on --git-dir's own resolved value: real "
            "git mutates /primary here, not /worktree, even though -C is the option nearer the "
            "subcommand"
        ),
    ),
    (
        "git -C /a -C /worktree reset --hard origin/main",
        "/primary",
        {"/a": True, "/worktree": False},
        None,
        "allow",
        "multiple -C options compose sequentially, the last (absolute) one replacing the running directory outright, matching real git's own repeated -C semantics",
    ),
    (
        "git -C /repos -C sub/primary reset --hard origin/main",
        "/somewhere-else",
        {"/repos/sub/primary": True, "/somewhere-else": False},
        None,
        "deny",
        "a relative -C after an earlier -C resolves against that earlier -C's own (absolute) result, not the session cwd",
    ),
    (
        "git --git-dir=/primary/.git reset --hard",
        "/worktree",
        {"/primary/.git": True, "/worktree": False},
        None,
        "deny",
        (
            "--git-dir alone, with no --work-tree, never relocates the mutation-target message "
            "(git's own fallback keeps the working tree at the effective cwd, here the linked "
            "worktree), but the primary-checkout test itself always keys on an explicit --git-dir "
            "when one is given, confirmed live: it names the repository actually mutated "
            "regardless of where the working tree files live"
        ),
    ),
    (
        "cd /primary && git reset --hard origin/main",
        "/somewhere-else",
        {"/primary": True, "/somewhere-else": False},
        None,
        "deny",
        "a leading cd resolves the target when the hook's own cwd points elsewhere entirely",
    ),
    (
        "git status && cd /primary && git reset --hard origin/main",
        "/somewhere-else",
        {"/primary": True, "/somewhere-else": False},
        None,
        "allow",
        "only a leading cd is read; one appearing after the first command is the accepted gap",
    ),
    (
        "bash -c 'cd /primary && git reset --hard origin/main'",
        "/somewhere-else",
        {"/primary": True, "/somewhere-else": False},
        None,
        "deny",
        "a bash -c wrapper is expanded the same way the GitHub-write rules already expand one, so it does not hide the mutation from this rule either",
    ),
    (
        "git fetch origin   # refresh the base\ngit reset --hard origin/main",
        "/primary",
        {"/primary": True},
        None,
        "deny",
        (
            "a mid-line # is not a comment starter left uncleared on the tokenizer's own shlex "
            "instance, confirmed live to silently swallow everything through the next newline "
            "and fuse the two lines into one `git fetch` invocation carrying the whole `reset "
            "--hard` as extra argv, hiding it from every tokenizer-based rule; a genuine # is "
            "now read as an ordinary character rather than a comment, so the newline separator "
            "and the second git invocation both survive"
        ),
    ),
    (
        "echo a#b && git -C /primary reset --hard origin/main",
        "/somewhere-else",
        {"/primary": True, "/somewhere-else": False},
        None,
        "deny",
        "a literal mid-word # (which real bash never treats as a comment starter either) no longer truncates the rest of the command and hides the mutation after it",
    ),
    (
        "git -C ~/repos/primary reset --hard origin/main",
        "/somewhere-else",
        {os.path.expanduser("~/repos/primary"): True, "/somewhere-else": False},
        None,
        "deny",
        "a ~-prefixed target expands the same way a shell would, since ~/repos/<Repo> is the fleet's own documented primary-checkout path convention",
    ),
    (
        "git -C /opt/$HOMEPATH/primary reset --hard origin/main",
        "/somewhere-else",
        {"/opt/$HOMEPATH/primary": True, "/somewhere-else": False},
        None,
        "deny",
        "$HOMEPATH is left unexpanded, matching this rule's own documented fail-open stance for a $VAR it cannot resolve, not misread as a prefix match on $HOME",
    ),
    (
        "git -C ../primary reset --hard origin/main",
        "/repos/worktree-task",
        {"/repos/primary": True, "/repos/worktree-task": False},
        None,
        "deny",
        "a relative -C (../primary) resolves against the session's own cwd (/repos/worktree-task -> /repos/primary), not wherever the hook process's own cwd happens to be",
    ),
    (
        "cd ../primary && git reset --hard origin/main",
        "/repos/worktree-task",
        {"/repos/primary": True, "/repos/worktree-task": False},
        None,
        "deny",
        (
            "a relative leading cd (../primary) is joined against the session's own cwd exactly "
            "like a relative -C is, not left unjoined and resolved against wherever the hook "
            "process's own OS-level cwd happens to be"
        ),
    ),
    (
        "git reset --hard origin/main",
        "/primary/.git",
        {"/primary/.git": True},
        None,
        "deny",
        "cwd inside .git itself still resolves as the primary checkout",
    ),
    (
        "git reset --hard origin/main",
        "/not-a-repo",
        {"/not-a-repo": None},
        None,
        "allow",
        "an unresolvable target fails open, precision over recall like every rule but 4",
    ),
    (
        "git --git-dir=/primary/.git --work-tree=/safe commit --allow-empty -m probe",
        "/safe",
        {"/primary/.git": True, "/safe": None},
        None,
        "deny",
        (
            "the CodeRabbit-reported gap: --work-tree names a directory that resolves as no git "
            "repository at all, but real git still mutates the repository --git-dir names -- "
            "confirmed live with the exact reproduction script CodeRabbit supplied -- so keying "
            "the primary-checkout test on --git-dir rather than the resolved --work-tree value is "
            "what catches this instead of failing open on the unresolvable work-tree"
        ),
    ),
    (
        "git --work-tree=/other-checkout reset --hard HEAD",
        "/primary",
        {"/primary": True, "/other-checkout": False},
        None,
        "deny",
        (
            "the mirror gap a local-strict-review pass found: --work-tree given with no "
            "--git-dir resolves as a linked worktree (or unresolvable), but real git still "
            "discovers the repository from the effective cwd with no --git-dir override, "
            "confirmed live to move the primary's own branch pointer back a commit and discard "
            "its own staged index entry even though the working-tree-file side effects land in "
            "the other checkout -- the identity dimension (effective cwd) catches this even "
            "though the file dimension (the resolved --work-tree value) alone would not"
        ),
    ),
]


# Rule 7: an unbounded shell wait. (command, expected_decision, label)
_WAIT_CASES = [
    (
        'until [ -s "/tmp/t/tasks/abc.output" ]; do sleep 30; done',
        "deny",
        "the incident: a poll on a file the awaited process may never write",
    ),
    (
        "while ! gh pr checks 5 | grep -q COMPLETED; do sleep 60; done",
        "deny",
        "a review wait whose condition can stay false forever",
    ),
    ("while true; do sleep 30; done", "deny", "the shape with no condition to become true at all"),
    (
        "while ! gh pr checks 5 --watch; do sleep 30; done  # the PR's checks",
        "deny",
        "an apostrophe in a trailing comment leaves the quoting unparseable, which must not hide the loop",
    ),
    (
        "while ! gh pr checks 5 --watch; do sleep 30; done  # the PRs checks",
        "deny",
        "the same loop and comment with no apostrophe",
    ),
    (
        "until [ -f x ]; do sleep 30; done  # the PR's checks",
        "deny",
        "an unbounded loop the fallback reads directly is still seen beside an apostrophe in a trailing comment",
    ),
    (
        "timeout 600 until [ -f x ]; do sleep 30; done  # the PR's checks",
        "allow",
        "the same loop under a timeout bound stays accepted beside an apostrophe in a trailing comment",
    ),
    (
        'echo "a\n# b"\nwhile true; do sleep 5; done  # the PR\'s checks',
        "deny",
        "a quote carried across lines still leaves the comment after it stripped and the loop seen",
    ),
    (
        'echo "a\n<<EOF # "\nwhile true; do sleep 5; done\nEOF',
        "deny",
        "a heredoc marker on a line an earlier quote opened hides no loop from the comment strip",
    ),
    (
        "echo it's <<EOF\n'; while true; do sleep 5; done\nEOF",
        "deny",
        "a heredoc marker inside an unclosed quote opens no heredoc, so the loop after the quote closes is seen",
    ),
    (
        "echo \"$(date)\" 'x\nwhile true; do sleep 5; done\n' # it's",
        "deny",
        "loop text inside a quote an earlier line opened still denies, since the line fallback's tokens are kept beside the scan's",
    ),
    (
        "(true)#'\nwhile true; do sleep 1; done\n#'\n# it's",
        "deny",
        "a `#` the scan misreads as text after a subshell's `)` does not hide the loop the line fallback sees",
    ),
    (
        "(true)#'\necho 'a\nb'; while true; do sleep 1; done # it's\n# don't",
        "deny",
        "a loop only the comment-stripped whole-command reading sees is still seen where the scan misreads a `#`",
    ),
    (
        'echo "$(echo case) \'"\nwhile true; do sleep 1; done\necho "\' esac)" # it\'s',
        "deny",
        "a `case` argument the scan misreads as a keyword does not hide the loop the line fallback sees",
    ),
    (
        'cat <<EOF "$(echo "\'")"\nwhile true; do sleep 1; done\nEOF',
        "deny",
        "a loop the line reading's heredoc strip keeps is denied where the scan's strip removes it",
    ),
    (
        "cat <<A | ${x:-;} bash\ncat <<B\nA\nwhile true; do sleep 1; done\nB",
        "deny",
        "a heredoc the scan reads as fed to a shell does not let a later body line's heredoc hide the loop",
    ),
    (
        "cat <<A `x;` bash\ncat <<B\nA\nwhile true; do sleep 1; done\nB",
        "deny",
        "a backquote the line reading splits does not let a later body line's heredoc hide the loop",
    ),
    (
        "while ! [[ $x =~ a|#b ]]; do sleep 30; done  # the PR's checks",
        "deny",
        "a `#` inside a `=~` operand is text, so the loop around the test is seen",
    ),
    (
        "while true; do sleep 1; done # it's \\note",
        "deny",
        "a backslash inside the comment itself does not hide the loop before it",
    ),
    (
        'echo "$(date)"; while ! gh pr checks 5 --watch; do sleep 30; done  # it\'s',
        "deny",
        "a command substitution elsewhere on the line does not hide the loop",
    ),
    (
        'echo "$(echo "\'")"; while true; do sleep 5; done; echo "\'"  # it\'s',
        "deny",
        "a double quote nested in a substitution inside a double quote does not hide the loop",
    ),
    (
        "echo ${x#a}; while true; do sleep 1; done # it's",
        "deny",
        "a `#` inside a parameter expansion is text, so the loop after it is seen",
    ),
    (
        "echo @(a|#b); while true; do sleep 1; done # it's",
        "deny",
        "a `#` inside an extglob pattern is text, so the loop after it is seen",
    ),
    (
        "(( x = 16#ff )); while true; do sleep 1; done # it's",
        "deny",
        "a `#` inside arithmetic is text, so the loop after it is seen",
    ),
    (
        "echo `date`; while true; do sleep 1; done # it's",
        "deny",
        "a backquote substitution elsewhere on the line does not hide the loop",
    ),
    (
        "printf %s \"$(cat <<'EOF'\nIt's done.\nEOF\n)\"\nwhile true; do sleep 5; done\necho \"Fix (it's broken)\"  # don't",
        "deny",
        "a quote in a heredoc body inside a substitution does not span past the body",
    ),
    (
        'printf \'%s\\n\' "$(git log -1 --format="%an\'s")" # the <<EOF form\nwhile true; do sleep 5; done',
        "deny",
        "a heredoc marker inside a comment opens no heredoc, so the loop after it is seen",
    ),
    (
        "echo $$'\\'; while true; do sleep 5; done; echo 'x' # it's",
        "deny",
        "a `$$` before a quote is the process id, so the quote after it honors no escape",
    ),
    (
        "grep -F '[[' f; echo =~ x|while true; do sleep 5; done # it's",
        "deny",
        "a quoted `[[` argument opens no conditional, so a later `=~` swallows nothing",
    ),
    (
        "echo \"$(case $x in a) echo \"'\";; esac)\"\nwhile true; do sleep 1; done\n# it's $'\\''",
        "deny",
        "a `case` pattern's `)` inside a substitution does not close it",
    ),
    (
        "((git fetch) && echo x) # it's\nwhile true; do sleep 1; done\n# it's $'\\''",
        "deny",
        "a `((` that closes on a lone `)` is nested subshells, not arithmetic",
    ),
    (
        "cat <<''\nit's\n\nwhile true; do sleep 1; done\n# it's $'\\''",
        "deny",
        "an empty quoted heredoc delimiter still opens a heredoc, ending at the first empty line",
    ),
    (
        "out=\"$(tr a b <<<'EOF'\n)\"\nwhile true; do sleep 1; done\nEOF\n)\" # it's $'\\''",
        "deny",
        "a herestring inside a substitution is not a heredoc",
    ),
    (
        "echo a\r# ; while true; do sleep 1; done\n# it's",
        "deny",
        "a carriage return is a word character, so a `#` after one opens no comment",
    ),
    (
        "echo $(date)#x ; while true; do sleep 1; done\n# it's",
        "deny",
        "a `#` glued to a substitution's `)` is text, not a comment",
    ),
    (
        "echo do [[ x =~ a|while true; do sleep 1; done # it's",
        "deny",
        "a keyword spelled as an argument does not put a later `[[` in command position",
    ),
    (
        "echo ${x:-{}\nwhile true; do sleep 1; done\necho } # it's $'\\''",
        "deny",
        "a `{` inside a parameter expansion does not nest, so its first `}` closes it",
    ),
    (
        "bash -c 'until [ -f /tmp/done ]; do sleep 10; done'",
        "deny",
        "a wrapper payload is read the same as a bare command line",
    ),
    (
        "until [ -f /tmp/done ]; do sleep 5; done &",
        "deny",
        "backgrounding the loop is what makes it outlive the turn, not what excuses it",
    ),
    (
        "timeout --help bash -c 'until [ -f x ]; do sleep 1; done'",
        "deny",
        "a timeout carrying no duration is not a bound",
    ),
    (
        "timeout 5 echo hi && until [ -f x ]; do sleep 5; done",
        "deny",
        "a timeout at the loop's own level bounds nothing, a loop keyword being no command to run",
    ),
    (
        "timeout 5 echo hi && bash -c 'until [ -f x ]; do sleep 5; done'",
        "deny",
        "and a timeout running something else does not reach a wrapper later on the line",
    ),
    (
        "timeout 600 bash -c \"bash -c 'until [ -f x ]; do sleep 5; done'\"",
        "allow",
        "a real bound is inherited through a nested wrapper",
    ),
    (
        'eval "until true; do sleep 1; done"',
        "deny",
        "an eval argument is shell text bash runs, read the same as a wrapper payload",
    ),
    (
        'eval until [ -f y ";" -lt 5 ]\\; do sleep 1\\; done',
        "deny",
        "a quoted separator becomes one when eval joins and rereads its arguments, so the comparison is no test's",
    ),
    (
        "eval -- 'while true; do sleep 1; done'",
        "deny",
        "eval drops one leading double dash before running the rest",
    ),
    (
        "echo eval 'until [ -f x ]; do sleep 5; done'",
        "allow",
        "an eval named as an argument runs nothing",
    ),
    (
        "timeout 600 bash -c 'eval \"until [ -f x ]; do sleep 5; done\"'",
        "allow",
        "an eval runs in the bounded shell, so it inherits that shell's bound",
    ),
    (
        "eval 'until [ \"$i\" -lt 5 ]; do sleep 1; done'",
        "allow",
        "an eval payload's own arithmetic guard bounds it",
    ),
    (
        'eval until false ";" do sleep 1 ";" done\necho "$(echo "it\'s")"',
        "deny",
        "where the quoting is unknown an eval payload runs to the end rather than stopping at a quoted separator",
    ),
    (
        'eval "$(ssh-agent -s)"\ntimeout 60 bash -c \'cd /w; until [ -f x ]; do sleep 5; done\'\necho "$(echo "it\'s")"',
        "allow",
        "where the quoting is unknown an eval payload still ends at its own line, so a later line keeps its quoting",
    ),
    (
        'eval >/dev/null \'until false; do sleep 1; done\'\necho "$(echo "it\'s")"',
        "deny",
        "where the quoting is unknown an eval's redirection still binds the whole payload rather than opening it",
    ),
    (
        'eval true; eval \'until false; do sleep 1; done\'\necho "$(echo "it\'s")"',
        "deny",
        "where the quoting is unknown an eval after a separator is read itself, its own words kept apart",
    ),
    (
        "eval echo;>/dev/null eval 'until false; do sleep 1; done'",
        "deny",
        "a separator fused to a redirection ends an eval's arguments, so the eval after it is a command",
    ),
    (
        "eval echo &>/dev/null eval 'until false; do sleep 1; done'",
        "allow",
        "`&>` is a redirection whole, so the eval after it is an argument and its loop's quoting is gone",
    ),
    (
        "while true; do eval 'sleep 5'; done",
        "deny",
        "a sleep an eval runs is a sleep, the same as one a wrapper runs",
    ),
    (
        "eval \"eval 'until [ -f x ]; do sleep 5; done'\n# until [ -f x ]; do sleep 5; done\"",
        "deny",
        "a nested eval is read from its own arguments, whatever text a later line repeats",
    ),
    (
        "while true; do eval \"eval 'sleep 1'\necho sleep 1\"; done",
        "deny",
        "a nested eval's sleep is read from its own arguments, whatever an echo after it repeats",
    ),
    (
        "eval 'yes | while read -r l; do sleep 1; done' < f",
        "deny",
        "a redirection on an eval binds the whole payload, as it binds a group, so the pipe still feeds the loop",
    ),
    (
        'while [ -f x ]; do eval "$step"; echo "a; sleep 1"; done',
        "allow",
        "an eval's payload ends at its own separator, so a sleep quoted in a later command stays text",
    ),
    (
        "echo ';' x eval 'until false; do sleep 1; done'",
        "allow",
        "a quoted separator before an argument named eval opens no command",
    ),
    (
        "echo ';>' eval 'until false; do sleep 1; done'",
        "allow",
        "and neither does a quoted separator fused to a redirection",
    ),
    (
        "while [ -f x ]; do echo ';>' eval 'sleep 1'; done",
        "allow",
        "and an eval named that way in a loop body runs no sleep",
    ),
    (
        "echo \";\" eval 'until false; do sleep 1; done'",
        "allow",
        "a quoted separator just before eval leaves it an argument",
    ),
    (
        'while [ -f x ]; do bash -c \'eval "$s"; logger "a; sleep 1"\'; done',
        "allow",
        "a wrapper payload's eval in a loop body is read with the payload's own quoting",
    ),
    (
        "while [ -f x ]; do echo ';>' sleep 1; done",
        "allow",
        "a quoted fused separator before an argument named sleep runs no sleep",
    ),
    (
        "timeout 60 eval 'until [ -f x ]; do sleep 5; done'",
        "allow",
        "an external launcher cannot run the eval builtin, so it runs nothing",
    ),
    (
        "nohup eval 'until [ -f x ]; do sleep 5; done'",
        "allow",
        "and nohup cannot either",
    ),
    (
        "while eval 'until false; do sleep 1; done' && [ \"$n\" -lt 3 ]; do n=$((n+1)); done",
        "deny",
        "an eval in a bounded loop's condition still runs its own unbounded loop",
    ),
    (
        "coproc eval 'until false; do sleep 1; done'",
        "deny",
        "a coprocess runs the eval builtin",
    ),
    (
        "time -p eval 'until false; do sleep 1; done'",
        "deny",
        "a runner's -p option still runs the eval",
    ),
    (
        "command -p -- eval 'until false; do sleep 1; done'",
        "deny",
        "and so do command's -p and --",
    ),
    (
        "command -v eval 'until false; do sleep 1; done'",
        "allow",
        "command -v only names the eval",
    ),
    (
        "\"command\" eval 'until false; do sleep 1; done'",
        "deny",
        "a quoted command still finds the builtin and runs the eval",
    ),
    (
        "builtin -p eval 'until false; do sleep 1; done'",
        "allow",
        "builtin takes no -p, so it runs nothing",
    ),
    (
        "FOO+=1 eval 'until false; do sleep 1; done'",
        "deny",
        "an appending assignment still runs the eval",
    ),
    (
        "a[0]=1 eval 'until false; do sleep 1; done'",
        "deny",
        "and so does an array element assignment",
    ),
    (
        "{fd}>f eval 'until false; do sleep 1; done'",
        "deny",
        "a named fd before a redirection still runs the eval",
    ),
    (
        "echo x;>f eval 'until false; do sleep 1; done'",
        "deny",
        "a separator fused to a redirection opens the eval's run",
    ),
    (
        "function f { eval 'until false; do sleep 1; done'; }; f",
        "deny",
        "a function body's group runs the eval",
    ),
    (
        "coproc NAME { eval 'until false; do sleep 1; done'; }",
        "deny",
        "and so does a named coprocess's group",
    ),
    (
        "FOO=1 time eval 'until false; do sleep 1; done'",
        "allow",
        "time after an assignment is the external time, which cannot run the eval",
    ),
    (
        "command time -p eval 'until false; do sleep 1; done'",
        "allow",
        "and so is time after command",
    ),
    (
        "a['k]']=1 eval 'until false; do sleep 1; done'",
        "deny",
        "a subscript holding a quoted ] is still an assignment",
    ),
    (
        "time -p { eval 'until false; do sleep 1; done'; }",
        "deny",
        "time's options before a group still leave its eval running",
    ),
    (
        "time -- ! eval 'until false; do sleep 1; done'",
        "deny",
        "and so does -- before a negation",
    ),
    (
        "a[\"x\ny\"]=1 eval 'until false; do sleep 1; done'",
        "deny",
        "a subscript holding a quoted newline is still an assignment",
    ),
    (
        ">eval 'until false; do sleep 1; done'",
        "allow",
        "an eval that is a redirection's target is a file name",
    ),
    (
        "{ >eval 'until false; do sleep 1; done'; }",
        "allow",
        "and so is one inside a group",
    ),
    (
        "FOO='a b' eval 'until false; do sleep 1; done'",
        "deny",
        "an assignment whose value is quoted still runs the eval",
    ),
    (
        "'FOO=1' eval 'until false; do sleep 1; done'",
        "allow",
        "a quoted assignment is a command named FOO=1, so the eval is its argument",
    ),
    (
        "\u0662>f eval 'until false; do sleep 1; done'",
        "allow",
        "a non-ASCII digit is no fd, so it is the run's command",
    ),
    (
        "GIT_PAGER=\"/bin/cat\" bash -lc 'until gh pr checks 5; do sleep 30; done'",
        "deny",
        "a quoted assignment value before a shell wrapper leaves the wrapper the command",
    ),
    (
        "echo $(echo)>f bash -c 'until false; do sleep 1; done'",
        "allow",
        "a redirection after a substitution leaves echo the run's command",
    ),
    (
        "command eval 'until false; do sleep 1; done'",
        "deny",
        "the command builtin does run eval",
    ),
    (
        "FOO=1 2>/dev/null eval 'until false; do sleep 1; done'",
        "deny",
        "an assignment and a redirection before eval leave it running",
    ),
    (
        "'sudo' bash -c 'until false; do sleep 1; done'",
        "deny",
        "a quoted launcher still runs the shell after it",
    ),
    (
        'eval "a\'b"; ' * 20 + 'eval \'until false; do sleep 1; done\'\necho "$(echo "it\'s")"',
        "deny",
        "where the quoting is unknown a loop after twenty evals is still read",
    ),
    (
        'eval "$(ssh-agent -s)";\ntimeout 60 bash -c \'cd /w; until [ -f x ]; do sleep 5; done\'\necho "$(echo "it\'s")"',
        "allow",
        "where the quoting is unknown an eval's line still ends where the lexer fuses its newline into a `;`",
    ),
    (
        'eval "$(ssh-agent -s)" &&\ntimeout 60 bash -c \'cd /w; until [ -f x ]; do sleep 5; done\'\necho "$(echo "it\'s")"',
        "allow",
        "and where it fuses the newline into a `&&`",
    ),
    (
        """eval until [ '"$i"' 2> ";" -lt 5 ]\\; do sleep 1\\; done""",
        "allow",
        "a quoted redirection target names a file rather than joining the payload as a separator",
    ),
    (
        "timeout 0 bash -c 'until [ -f x ]; do sleep 30; done'",
        "deny",
        "GNU timeout documents a zero duration as disabling the timeout, so it is not a bound",
    ),
    (
        "timeout 600 bash -c 'until [ -f x ]; do sleep 30; done &'",
        "deny",
        "a backgrounded loop outlives the shell the timeout bounds, so the timeout bounds nothing",
    ),
    (
        "timeout 600 bash -c 'until [ -f x ]; do sleep 30; done > /tmp/log &'",
        "deny",
        "a redirection before the ampersand does not stop it backgrounding the loop",
    ),
    (
        "timeout 600 bash -c 'until [ -f x ]; do sleep 30; done 2>/dev/null &'",
        "deny",
        "nor does one carrying its file descriptor as a token of its own",
    ),
    (
        "timeout 600 bash -c 'until [ -f x ]; do sleep 30; done >> a 2>&1 &'",
        "deny",
        "nor two of them together",
    ),
    (
        "timeout 600 bash -c 'until [ -f x ]; do sleep 30; done 2>&1'",
        "allow",
        "while a redirection with no ampersand after it backgrounds nothing",
    ),
    (
        "while read l; do sleep 1; done < f &",
        "allow",
        "a loop bounded by its input stays bounded backgrounded, since that bound needs no signal",
    ),
    (
        "gtimeout 60 bash -c 'until [ -f x ]; do sleep 5; done'",
        "allow",
        "gtimeout is the only GNU timeout a macOS host has",
    ),
    (
        "sudo timeout 60 bash -c 'until [ -f x ]; do sleep 5; done'",
        "allow",
        "a privilege prefix does not hide the timeout behind it",
    ),
    (
        "TMPDIR=/tmp timeout 60 bash -c 'until [ -f x ]; do sleep 5; done'",
        "allow",
        "nor does an assignment prefix",
    ),
    (
        "until [ -f x ]; do /usr/bin/env sleep 1; done",
        "deny",
        "a prefix is recognized path-qualified, the way the exe helpers already recognize one",
    ),
    (
        "until [ -f x ]; do FOO=1 sleep 1; done",
        "deny",
        "and an assignment before the sleep is not an argument to it",
    ),
    (
        'while IFS= read -r pr; do gh pr view "$pr"; sleep 2; done < prs.txt',
        "allow",
        "a loop reading its input ends when the input does, and throttling it is ordinary work",
    ),
    (
        "while grep -q read file; do sleep 5; done",
        "deny",
        "while a read named later in the condition is an argument and bounds nothing",
    ),
    (
        "for ((;;)); do sleep 30; done",
        "deny",
        "the arithmetic for runs forever exactly as while true does",
    ),
    (
        "for (( i=0; i<10; i++ )); do sleep 1; done",
        "allow",
        "and carries its own guard when it has one",
    ),
    (
        "grep -q x <<< foo\nuntil [ -f y ]; do sleep 30; done",
        "deny",
        "a herestring is not a heredoc, and reading one as one deleted the command after it",
    ),
    (
        "echo $((a<<b))\nuntil [ -f y ]; do sleep 30; done",
        "deny",
        "nor is an arithmetic shift",
    ),
    (
        "echo $(( 1 << shift ))\nuntil [ -f y ]; do sleep 30; done",
        "deny",
        "nor a spaced one, which a raw-text read took for an opener and dropped the command after",
    ),
    (
        "while [ -e /x ] <<EOF ; : $((0))\n[ x -lt 5 ]\nEOF\ndo sleep 1; done",
        "deny",
        "a real heredoc on a line holding (( keeps no body to vouch for the loop's bound",
    ),
    (
        "while [ -e /x ] ; : $(( 1 << n )) <<EOF\n[ x -lt 5 ]\nEOF\ndo sleep 1; done",
        "deny",
        "nor does one after a shift on its line, since every << there is read as the opener in turn",
    ),
    (
        "while [ -e /x ] <<EOF ; echo '(('\n[ x -lt 5 ]\nEOF\ndo sleep 1; done",
        "deny",
        "nor does one beside a quoted ((, which tokenizes as the arithmetic does",
    ),
    (
        "while [ -e /x ] <<A ; : $((0))\n[ x -lt 5 ]\nA\n: $((0)) <<B\n[ y -lt 5 ]\nB\ndo sleep 1; done",
        "deny",
        "nor do two such lines, each read both ways together with the other",
    ),
    (
        "cat <<A <<B\na\nA\ncat <<X\nB\nwhile [ ! -f /x ]; do sleep 1; done\nX",
        "deny",
        "a line opening two heredocs strips both bodies, so a second body's opener hides no loop",
    ),
    (
        ": $((0)) <<EOF <<EOF\na\nEOF\ncat <<X\nEOF\nwhile [ ! -f /x ]; do sleep 1; done\nX",
        "deny",
        "nor does one holding (( whose two bodies share a tag",
    ),
    (
        ": $((0)) <<A <<B\nB\nA\ncat <<X\nB\nwhile [ ! -f /x ]; do sleep 1; done\nX",
        "deny",
        "nor one holding (( whose first body holds the second tag's line",
    ),
    (
        "cat <<A; bash <<B\na\nA\nwhile [ ! -f /x ]; do sleep 1; done\nB",
        "deny",
        "a second body fed to a shell is still read as the script it is",
    ),
    (
        "bash <<A; cat <<B\necho hi\nA\ncat <<X\nB\nwhile [ ! -f /x ]; do sleep 1; done\nX",
        "deny",
        "and a first one fed to a shell is kept whole while the data body after it is stripped",
    ),
    (
        ": $((0)); bash <<A; cat <<B\necho hi\nB\nA\ncat <<X\nB\nwhile [ ! -f /x ]; do sleep 1; done\nX",
        "deny",
        "on a line holding (( too",
    ),
    (
        "cat <<A >/dev/null; x=$(cat <<B\nb\nB\n)\na\nA\nwhile [ ! -f /x ]; do sleep 1; done",
        "deny",
        "a heredoc inside a substitution is read before it closes, not queued after the line's own",
    ),
    (
        "cat <<A >/dev/null; x=`cat <<B\nb\nB\n`\na\nA\nwhile [ ! -f /x ]; do sleep 1; done",
        "deny",
        "and so is one inside a backquote",
    ),
    (
        "cat <<A >/dev/null; echo $[ 1 << X ]\na\nA\nwhile [ ! -f /x ]; do sleep 1; done\nX",
        "deny",
        "a shift inside $[ ] is read as one inside (( )) is",
    ),
    (
        'cat > "$(pwd)/notes.md" <<EOF\nwhile [ ! -f /x ]; do sleep 1; done\nEOF',
        "allow",
        "and a document written to a path holding a substitution still strips its body",
    ),
    (
        "cat <<A >/dev/null; grep -c '<<' B\na\nA\nwhile [ ! -f /x ]; do sleep 1; done\nB",
        "deny",
        "a quoted << after a real heredoc is also read as the text it is, so it strips no command",
    ),
    (
        "n=$((1)); "
        + "; ".join(f"cat <<T{k} >f{k}.md" for k in range(5))
        + "\n"
        + "\n".join(f"T{k}" for k in range(5))
        + "\necho 'use while with sleep and a bound'",
        "allow",
        "and five empty bodies on a line holding (( build one reading rather than passing the limit",
    ),
    (
        'i=0; while [ "$i" -lt 5 ]; do cat <<EOF ; sleep 1; i=$((i+1))\nbody\nEOF\ndone',
        "allow",
        "and a bounded loop holding a heredoc and (( on one body line stays allowed",
    ),
    (
        "timeout 600 bash -c '\nwhile ! [ -f /x ]; do sleep 1; done\necho $(( 1 << n ))\necho fin\n'",
        "allow",
        "a shift inside a quoted payload opens nothing when no later line closes it",
    ),
    (
        "\n".join(["echo $(( a << b ))", "x", "b"] * 7 + ["while [ ! -f /x ]; do sleep 1; done"]),
        "deny",
        "a waiting command past the reading limit is denied rather than read one way",
    ),
    (
        "\n".join(
            ["echo $(( a << b ))", "x", "b"] * 7
            + ["cat > a.md <<EOF", "don't", "EOF", "while [ ! -f /x ]; do sleep 1; done"]
            + ["cat > b.md <<EOF", "it's", "EOF"]
        ),
        "deny",
        "nor does a quote pairing across two bodies hide the sleep from that limit",
    ),
    (
        "\n".join(["echo $(( a << b ))", "x", "b"] * 7),
        "allow",
        "and one that never sleeps is allowed, since it holds no wait to judge",
    ),
    (
        "\n".join(["echo $(( a << b ))", "x", "b"] * 7 + ["sleep 1"]),
        "allow",
        "nor one that sleeps and names no loop keyword, since a wait needs both",
    ),
    (
        "\n".join(['cat > "part$((N+1)).md" <<EOF', "text", "EOF"] * 5 + ["sleep 2"]),
        "allow",
        "so five numbered heredoc writes followed by a pause stay allowed past the limit",
    ),
    (
        "\n".join(
            ['cat > "part$((N+1)).md" <<EOF', "While it loads, wait.", "EOF"] * 5 + ["sleep 2"]
        ),
        "allow",
        "even where their bodies hold a capitalized loop word, since bash reads a keyword in its case",
    ),
    (
        "while [ ! -f /x ]; do sl$'e'ep 1; done # it's",
        "deny",
        "and a `sleep` spelled through a `$'...'` span is still read as one",
    ),
    (
        "\n".join(
            ["echo $(( a << b ))", "b"] * 7
            + ["timeout 5 bash -c 'while [ ! -f /x ]; do sleep 1; done'"]
        ),
        "allow",
        "and a `<<` whose body is empty forks nothing, so a bounded wait after seven is still read",
    ),
    (
        "git commit -m 'Explain the << EOF form'\nuntil [ -f y ]; do sleep 30; done",
        "deny",
        "and a `<<` inside a quoted value is the text it is",
    ),
    (
        "cat > d.md << EOF\nuntil [ -f x ]; do sleep 5; done\nEOF",
        "allow",
        "a space between the redirection and its tag still opens a heredoc",
    ),
    (
        "bash -c 'echo hi' && cat > docs/waits.md <<'EOF'\nuntil [ -f x ]; do sleep 5; done\nEOF",
        "allow",
        "whether a shell reads the body is the redirection's own command, not the line mentioning one",
    ),
    (
        "while sleep 30; do gh pr checks 5 | grep -q COMPLETED && break; done",
        "deny",
        "the poll-forever idiom sleeps in its condition, which is a sleep like any other",
    ),
    (
        "while true; do bash -c 'sleep 30'; done",
        "deny",
        "and a sleep the body hands to a wrapper is still the body sleeping",
    ),
    (
        "while true; do bash -c 'echo a; sleep 30'; done",
        "deny",
        "wherever in that payload it sits, which re-joining the tokens once lost",
    ),
    (
        "while true; do bash -c 'pkill sleep'; done",
        "allow",
        "while a sleep named as an argument to something else runs none",
    ),
    (
        "cat <<'EOF' | bash\nuntil [ -f x ]; do sleep 30; done\nEOF",
        "deny",
        "a heredoc piped into a shell is a script that shell runs, whatever owns the redirection",
    ),
    (
        "nice -n 10 bash <<'EOF'\nuntil [ -f x ]; do sleep 30; done\nEOF",
        "deny",
        "and a prefix before that shell does not hide it",
    ),
    (
        "cat << - EOF\ndoc line\n-\nuntil [ -f x ]; do sleep 30; done\nEOF",
        "deny",
        "a bare dash token is left unread, since bash reads that spelling's delimiter as the dash",
    ),
    (
        "timeout 600 until [ -f x ]; do sleep 30; done",
        "allow",
        "bash rejects this before it runs, so it is a syntax error rather than a wait to judge, and the denial no longer names it",
    ),
    (
        "timeout -k 30 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "an option taking a separate value still leaves the duration findable",
    ),
    (
        "timeout -s 0 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "signal 0 is delivered to no process, so a timeout sending it expires and leaves its child running",
    ),
    (
        "timeout --signal 0 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and the long option spelling sends the same nothing",
    ),
    (
        "timeout --signal=0 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "as does the long option carrying its value inline",
    ),
    (
        "timeout -s0 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and the short option carrying its value attached",
    ),
    (
        "timeout -vs0 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "including at the end of a short option cluster",
    ),
    (
        "timeout --sig=0 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and a long option abbreviated as getopt_long accepts it",
    ),
    (
        "timeout -s EXIT 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and EXIT, the name GNU timeout gives signal 0",
    ),
    (
        "timeout -s sig00 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "in any case, with the SIG prefix and leading zeros",
    ),
    (
        "timeout -s KILL -s 0 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "the last signal option given is the one timeout sends",
    ),
    (
        "timeout -s 0 -s KILL 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "so a signal 0 overridden by a later one is still a bound",
    ),
    (
        "timeout -s KILL 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "an ordinary signal is still a bound",
    ),
    (
        "timeout --signal=TERM 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "in the inline long spelling too",
    ),
    (
        "timeout -s 128 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a number GNU timeout masks to 0 is signal 0 too, as 128 is",
    ),
    (
        "timeout -s 256 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and 256, masked from 255 up",
    ),
    (
        "timeout -s " + "0" * 5000 + " 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a zero past the digits int() converts is still signal 0, and still read without a crash",
    ),
    (
        "timeout -s 1" + "0" * 5000 + " 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "while a number that long past its leading zeros is no signal at all",
    ),
    (
        "timeout -s 129 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "while one masking to a real signal, as 129 does to HUP, is a bound",
    ),
    (
        "timeout -s 0 -k 30 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "a kill-after follows signal 0 with a SIGKILL, so the pair is a bound",
    ),
    (
        "timeout --kill-after=30 --signal 0 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "in either order and either spelling",
    ),
    (
        "timeout -s 0 -k 0 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "while a zero kill-after disables it and bounds nothing",
    ),
    (
        "timeout -s 0 900 timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "a signal-0 timeout running another timeout is bounded by the inner one",
    ),
    (
        "timeout -s 0 900 nice timeout -s 0 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "while an inner timeout sending signal 0 too bounds nothing either",
    ),
    (
        "timeout -s 0 900 nice timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "and a prefix taking no argument between the two leaves the inner one as the bound",
    ),
    (
        "timeout -s 0 900 timeout -s 0 800 timeout 700 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "every outer timeout sending signal 0 leaves the innermost one as the bound",
    ),
    (
        "timeout -s 0 -k 30 900 timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "an outer kill-after is not inert, so the nesting is read as no bound, a declared false deny here",
    ),
    (
        "timeout 900 timeout -s 0 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and neither is an outer SIGTERM, a declared false deny",
    ),
    (
        "timeout -k 30 900 timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "nor one carrying a kill-after, however the inner one is spelled",
    ),
    (
        "timeout -s KILL 10 timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "an outer signal can end the inner timeout before its deadline and leave the loop running",
    ),
    (
        "timeout 900 time timeout -s 0 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a command prefix between the two changes nothing about the outer one",
    ),
    (
        "timeout -s KILL 10 nice -n 5 timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "nor does a prefix taking an argument between the two",
    ),
    (
        "timeout -s 0 900 nice -n 5 timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "which is read as no bound even behind a signal-0 outer one, a declared false deny",
    ),
    (
        "timeout -s 0 900 foo -s 0 5 timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and a word naming timeout after any other command is read as nesting too",
    ),
    (
        "timeout -s 0 -k .5 900 timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "an outer kill-after counts in any spelling, since GNU timeout reads more than the duration form",
    ),
    (
        "timeout -s 10 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "and a number is signal 0 only where GNU timeout masks it to 0, which 10 is not",
    ),
    (
        "timeout -s \"$SIG\" 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a signal the shell expands at run time may be signal 0, so it is no bound",
    ),
    (
        "timeout -s \"$SIG\" -k 30 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and no later kill-after rescues it, since the rewrite may split into more words",
    ),
    (
        "timeout -s {0..0} 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and a brace expansion the shell rewrites to 0 is read the same way",
    ),
    (
        "timeout -s ~ 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "as is a tilde, which the shell rewrites to HOME",
    ),
    (
        "timeout -${F}0 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and an option word the shell may rewrite into -s0 names no signal either",
    ),
    (
        "timeout -${F}0 -k 30 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "nor here, where a rewrite into a duration and a command ends option parsing",
    ),
    (
        "timeout -s 0 -$X 10 timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and behind an outer timeout it may be a kill-after, so the nesting is read as no bound",
    ),
    (
        "timeout -$X -s 0 10 timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "even where a later literal -s 0 follows it",
    ),
    (
        "timeout -k $K 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a kill-after value the shell may split, as into 0 -s 0, bounds nothing",
    ),
    (
        "timeout -k 30 -s $SIG 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "nor does an earlier kill-after that a rewritten signal value may replace",
    ),
    (
        "timeout -k {0,-s0} 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and a brace expansion splitting into more options is read the same way",
    ),
    (
        "timeout -s KILL 10 $T 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a rewritable word past the duration is no bound, as one that may become timeout",
    ),
    (
        "timeout -s KILL 10 env X=$T 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and so is an assignment argument the shell splits after env",
    ),
    (
        "timeout -$X -k 30 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a rewritable option word is no bound whatever literal options follow it",
    ),
    (
        "timeout 900 env PATH=$HOME/bin bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a declared false deny, since the rewrite here yields a bound",
    ),
    (
        "timeout 900 env MSG='a*b' bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and so is a quoted word holding a rewrite character, which the shell leaves literal",
    ),
    (
        "env X=$T timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a rewritable word before the timeout is read too, since env splits it into a possible outer timeout",
    ),
    (
        "sudo X=$T timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "as sudo does",
    ),
    (
        "X=$T timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and an assignment prefix the shell does not split, a declared false deny",
    ),
    (
        "timeout -s KILL 10 $(true) timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a command substitution closing before the run leaves its start unknown",
    ),
    (
        "$(echo timeout -s KILL 10) timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and so does one that may print an outer timeout",
    ),
    (
        "{ timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'; }",
        "allow",
        "a bare { is the brace-group reserved word rather than a rewrite",
    ),
    (
        "x=$(timeout 900 bash -c 'until [ -f x ]; do sleep 60; done')",
        "allow",
        "a wrapper inside a substitution starts its run at the group it sits in",
    ),
    (
        "case $x in a) timeout 900 bash -c 'until [ -f x ]; do sleep 60; done';; esac",
        "deny",
        "a case pattern's ) before the run is a declared false deny",
    ),
    (
        "timeout -s KILL 10 $(true;) timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and so is one whose closing parenthesis the lexer fuses with an operator",
    ),
    (
        "timeout -s KILL 10 $(echo $(true)) timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "as a nested substitution's is",
    ),
    (
        "timeout -s KILL 10 /usr/bin/@(time)out 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a glob group closing before the run leaves its start unknown",
    ),
    (
        "timeout 900 grep -c \"(\" f; case a in a) bash -c 'until [ -f x ]; do sleep 60; done';; esac",
        "deny",
        "and a quoted ( never reaches across a separator to an earlier timeout",
    ),
    (
        "case foo in (a) timeout 900 bash -c 'until [ -f x ]; do sleep 60; done';; esac",
        "deny",
        "with or without its optional (",
    ),
    (
        "timeout -s KILL 10 nice$(true) timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a substitution fused into a word is read the same way",
    ),
    (
        "timeout -s KILL 10 $(echo \")\") timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "as is one holding a quoted parenthesis",
    ),
    (
        "timeout -s KILL 10 flock <(true) timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and a process substitution",
    ),
    (
        "timeout -s KILL 10 /usr/bin/@(nice) timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and a glob group that may name a prefix",
    ),
    (
        "x=$(date); timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "a ) fused with a separator ends the earlier run instead",
    ),
    (
        "f() { timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'; }",
        "allow",
        "and a function definition's () opens no group before the run",
    ),
    (
        "timeout -s KILL 10 $() timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "an empty substitution is no function definition",
    ),
    (
        "f ( ) { timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'; }",
        "allow",
        "while a spaced function definition is one",
    ),
    (
        "timeout -s KILL 10 env -u ';' timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a quoted operator is a word of the run rather than its end",
    ),
    (
        "timeout 900 sudo -p ')' bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "so a quoted ) closes no group",
    ),
    (
        "timeout -s KILL 10 env >timeout -us0 -u 5 timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a redirection in the run leaves its start unknown",
    ),
    (
        ">log timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and any redirection in the run is a declared false deny",
    ),
    (
        "timeout 900 true;>f bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "as one fused with a separator does",
    ),
    (
        "timeout 5>f 0 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "so a descriptor number is never read as the duration",
    ),
    (
        "timeout -s KILL 10 nice $() timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a $ never names a function, behind a prefix either",
    ),
    (
        "timeout -s KILL 10 env -u function $() timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "or after the word function",
    ),
    (
        "if true; then f() { timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'; }; fi",
        "allow",
        "while a function defined after then is one",
    ),
    (
        "shopt -s extglob\ntimeout -s KILL 10 /usr/bin/@(timeout) 800 bash -c 'until false; do sleep 1; done' # it's",
        "deny",
        "an extglob group the fallback lexer keeps in one word is read as a rewrite",
    ),
    (
        "mapfile -t lines < <(timeout 900 bash -c 'until [ -f x ]; do sleep 60; done')",
        "allow",
        "a run opening a process substitution starts after its <(",
    ),
    (
        "timeout -s KILL 10 $(true) @() timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "an extglob group is no function name",
    ),
    (
        "timeout -s KILL 10 env -u do @() timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "even after a word that may open a definition",
    ),
    (
        "timeout -s KILL 10 $(true) f() timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "and a name after a group's ) opens none",
    ),
    (
        "if f() { timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'; }; then f; fi",
        "allow",
        "a function defined after if is one",
    ),
    (
        "echo ';' timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "and a quoted separator after a command naming its arguments runs no shell",
    ),
    (
        "timeout 900 xargs -I{} bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "a brace pair with no comma or .. is no brace expansion",
    ),
    (
        "timeout -s KILL 10 env {Y={},ti}meout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "while a comma between the outer braces is one, whatever pair sits inside",
    ),
    (
        "timeout 900 env A=x~y bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "and a tilde inside a word is not expanded",
    ),
    (
        "timeout -s KILL 10 env A=~ timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "while one after = is",
    ),
    (
        "timeout -s KILL 10 =timeout 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "a leading = is rewritten into a command path by zsh, so it may name timeout",
    ),
    (
        "timeout -s KILL 10 /usr/bin/time?ut 800 bash -c 'until [ -f x ]; do sleep 60; done'",
        "deny",
        "as is a glob that may match it",
    ),
    (
        "timeout 900 env X=/timeout bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "while an assignment directly after the duration is skipped as a prefix",
    ),
    (
        "timeout 900 bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "the same wait with no signal option is bounded",
    ),
    (
        "timeout 900 nice bash -c 'until [ -f x ]; do sleep 60; done'",
        "allow",
        "and a command prefix between the timeout and the shell does not hide it",
    ),
    (
        "if timeout 600 bash -c 'until [ -f x ]; do sleep 30; done'; then echo MET; fi",
        "allow",
        "a run opening with a keyword is still that run",
    ),
    (
        "until [ -f /tmp/flag ]; do echo done; sleep 30; done",
        "deny",
        "the word done inside the body does not close the loop before its sleep is read",
    ),
    (
        "until [ -f x ]; do command sleep 1; done",
        "deny",
        "a sleep behind a command prefix is still a sleep",
    ),
    (
        "cat > notes.md <<'END-DOC'\nuntil [ -f x ]; do sleep 5; done\nEND-DOC",
        "allow",
        "a hyphenated heredoc tag is a heredoc, so a document using one is written rather than denied",
    ),
    (
        "cat > f <<'EOF'\n  EOF\nwhile true; do sleep 1; done\nEOF",
        "allow",
        "an indented line reading as the tag does not end a plain heredoc, so the rest stays data",
    ),
    (
        'i=0; while [ "$i" -lt 5 ]; do until [ -f x ]; do sleep 1; done; i=$((i+1)); done',
        "deny",
        "an unbounded inner wait is unbounded however bounded the loop around it is",
    ),
    (
        "until [ -f x ]; do /bin/sleep 5; done",
        "deny",
        "a path-qualified sleep is the same sleep",
    ),
    (
        "bash <<'EOF'\nuntil [ -f x ]; do sleep 5; done\nEOF",
        "deny",
        "a heredoc fed to a shell is the script that shell runs",
    ),
    (
        "timeout 600 bash -c 'until [ -f /tmp/done ]; do sleep 30; done'",
        "allow",
        "a timeout wrapper is the bound, inherited into the payload it wraps",
    ),
    (
        "timeout 10m bash -c 'while ! test -f x; do sleep 2; done'",
        "allow",
        "a suffixed duration is a duration",
    ),
    (
        'i=0; while [ "$i" -lt 120 ] && ! curl -sf http://x; do sleep 5; i=$((i+1)); done',
        "allow",
        "a counter the condition reads is the other bound",
    ),
    (
        "while (( SECONDS < 600 )); do sleep 10; done",
        "allow",
        "the arithmetic form of that same guard",
    ),
    (
        "while (( 1 << 1 )); do sleep 1; done",
        "deny",
        "a shift inside the arithmetic form is not a comparison and bounds nothing",
    ),
    (
        "while ! ls -lt out | grep -q result; do sleep 30; done",
        "deny",
        "a command flag spelling a comparison is no test builtin's operand and bounds nothing",
    ),
    (
        "while ! grep -le done log; do sleep 30; done",
        "deny",
        "`grep -le` names a pattern rather than comparing anything",
    ),
    (
        "while ! echo [ 1 -lt 2 ] | grep -q x; do sleep 30; done",
        "deny",
        "a bracket that is an argument rather than the command runs no test",
    ),
    (
        "until test $i -ge 3; do sleep 1; i=$((i+1)); done",
        "allow",
        "the `test` spelling of the test builtin carries the same bound",
    ),
    (
        "while [[ $i -lt 10 && ! -f x ]]; do sleep 1; i=$((i+1)); done",
        "allow",
        "a comparison inside `[[ ]]` bounds the loop, the `&&` inside it joining two tests",
    ),
    (
        "while [[ -f x && $i -lt 10 ]]; do sleep 1; i=$((i+1)); done",
        "allow",
        "a `&&` inside `[[ ]]` does not close it before a later comparison",
    ),
    (
        "while ! [ -f x ] && ls -lt out; do sleep 30; done",
        "deny",
        "a separator closes a `[` test, so a flag after it is still no bound",
    ),
    (
        "until [ $(date +%s) -ge $end ]; do sleep 5; done",
        "allow",
        "a command substitution operand does not end the test before its comparison",
    ),
    (
        "while [ $((i)) -lt 10 ]; do sleep 1; i=$((i+1)); done",
        "allow",
        "an arithmetic expansion operand does not end the test before its comparison",
    ),
    (
        "while [ $(a $(b)) -lt 1 ]; do sleep 1; done",
        "allow",
        "a nested substitution closing on one `))` token leaves the comparison after it credited",
    ),
    (
        'while [ \\( "$i" -lt 3 \\) ]; do sleep 1; i=$((i+1)); done',
        "allow",
        "a grouping parenthesis inside the test neither ends it nor hides its comparison",
    ),
    (
        "while ! [[ $(ls -lt out) == *x* ]]; do sleep 30; done",
        "deny",
        "a flag inside a substitution in a `[[ ]]` operand is that command's flag, not a comparison",
    ),
    (
        'while ! [ -n "$(ls -lt out)" ]; do sleep 30; done',
        "deny",
        "a quoted substitution is one operand, so its flag compares nothing",
    ),
    (
        "while /usr/bin/test $i -lt 3; do sleep 1; i=$((i+1)); done",
        "allow",
        "a path-qualified `test` is the same test",
    ),
    (
        "while builtin test $i -lt 3; do sleep 1; i=$((i+1)); done",
        "allow",
        "`builtin test` runs the same test builtin",
    ),
    (
        "while ! [ -n `ls -lt out` ]; do sleep 30; done",
        "deny",
        "a flag inside a backtick substitution is that command's flag, not a comparison",
    ),
    (
        "while [ `cat n` -lt 3 ]; do sleep 1; done",
        "allow",
        "a backtick operand ends before the comparison after it",
    ),
    (
        "while ! [[ -s <(ls -lt out) ]]; do sleep 30; done",
        "deny",
        "a flag inside a process substitution is that command's flag, not a comparison",
    ),
    (
        "while ! [[ $(true |(cat) | ls -lt out) ]]; do sleep 30; done",
        "deny",
        "a subshell fused to a pipe inside a substitution keeps the substitution open",
    ),
    (
        'while test "" != "$x" -a $i -lt 3; do sleep 1; i=$((i+1)); done',
        "allow",
        "an empty operand does not close a `test` invocation",
    ),
    (
        "while ! (test -f x) && ls -lt out; do sleep 30; done",
        "deny",
        "a subshell's closing parenthesis ends the `test` inside it, so a later flag is no bound",
    ),
    (
        "while ! [ -n x$(ls -lt out) ]; do sleep 30; done",
        "deny",
        "a substitution glued to a word is still a command of its own, so its flag compares nothing",
    ),
    (
        "while ! test -f x;(ls -lt out); do sleep 30; done",
        "deny",
        "a separator fused to a subshell ends the `test`, so the subshell's flag is no bound",
    ),
    (
        "while ! test -f x|(ls -lt out); do sleep 30; done",
        "deny",
        "a pipe fused to a subshell ends the `test`, so the subshell's flag is no bound",
    ),
    (
        "while ! [[ $(case a in a) ls -lt out;; esac) ]]; do sleep 30; done",
        "deny",
        "a `case` pattern's parenthesis inside a substitution does not end the substitution",
    ),
    (
        "while [ $(case a in (a) echo 1;; esac) -lt 3 ]; do sleep 1; done",
        "allow",
        "a substitution holding a `case` still ends at its own parenthesis before a comparison",
    ),
    (
        "while [ \"$c\" != '`' -a $i -lt 3 ]; do sleep 1; i=$((i+1)); done",
        "allow",
        "a quoted literal backtick with no partner opens no substitution to hide the comparison",
    ),
    (
        "while [[ -f x || $i -lt 3 ]]; do sleep 1; i=$((i+1)); done",
        "allow",
        "a `||` inside `[[ ]]` joins two tests rather than ending one",
    ),
    (
        "while ! test -n $(cat f); ls -lt out | grep -q x; do sleep 30; done",
        "deny",
        "a separator fused to a substitution's close ends the `test` too",
    ),
    (
        "while [[ $x =~ ^(a|b)$ && $i -lt 3 ]]; do sleep 1; i=$((i+1)); done",
        "allow",
        "a regex alternation's `|` inside `[[ ]]` does not end it",
    ),
    (
        "while [[ $x == @(a|b) && $i -lt 3 ]]; do sleep 1; i=$((i+1)); done",
        "allow",
        "an extglob alternation's `|` inside `[[ ]]` does not end it",
    ),
    (
        'while [ "$x" != ";" -a $i -lt 5 ]; do sleep 1; i=$((i+1)); done',
        "allow",
        "a quoted `;` is an operand rather than a separator",
    ),
    (
        'while [ "$c" != ")" -a $i -lt 5 ]; do sleep 1; i=$((i+1)); done',
        "allow",
        "a quoted `)` is an operand rather than a grouping parenthesis",
    ),
    (
        'while [[ $x != "|" && $i -lt 3 ]]; do sleep 1; i=$((i+1)); done',
        "allow",
        "a quoted `|` inside `[[ ]]` is a string",
    ),
    (
        'while ! ls $(test -f "(") -lt out; do sleep 30; done',
        "deny",
        "a quoted `(` opens no group to keep a substitution's `test` open past its close",
    ),
    (
        'while ! test -f "`"; grep "`" -le x log; do sleep 30; done',
        "deny",
        "two quoted backticks pair into no substitution to hide the separator between them",
    ),
    (
        'while true; do echo ";" done; sleep 1; done',
        "deny",
        "a `done` after a quoted `;` is an argument rather than the loop's close",
    ),
    (
        'while true; do echo x; "done"; sleep 1; done',
        "deny",
        "a quoted `done` is a command name rather than the loop's close",
    ),
    (
        'while ! echo ";" [ 1 -lt 2 ]; do sleep 30; done',
        "deny",
        "a bracket after a quoted `;` is an argument rather than a command",
    ),
    (
        'while ! test -n "\\"";"grep" -le x log; do sleep 30; done',
        "deny",
        "an escaped quote inside quotes shifts no real separator into a quoted operand",
    ),
    (
        'echo "say \\"hi\\""; while [ "$x" != ";" -a $i -lt 5 ]; do sleep 1; i=$((i+1)); done',
        "allow",
        "an escaped quote before the loop leaves its quoted `;` an operand",
    ),
    (
        'find . -name x -exec rm {} \\; ; while [ "$x" != ";" -a $i -lt 5 ]; do sleep 1; i=$((i+1)); done',
        "allow",
        "an escaped `;` before the loop leaves its quoted `;` an operand",
    ),
    (
        'while [ "$x" != ";" -a $i -lt 5 ]; do echo "\\"" ; sleep 1; i=$((i+1)); done',
        "allow",
        "an escaped quote in the body leaves the condition's quoted `;` an operand",
    ),
    (
        "echo 'it'\\''s'; while [ \"$x\" != \";\" -a $i -lt 5 ]; do sleep 1; i=$((i+1)); done",
        "allow",
        "a `'\\''` before the loop leaves its quoted `;` an operand",
    ),
    (
        'find . -name x -exec rm {} \\; ; while [ "$x" != ";" -a -f y ]; do sleep 1; done',
        "deny",
        "an escape before a quoted-operator loop with no comparison bounds nothing",
    ),
    (
        'echo "say \\"hi\\""; while [ "$x" != ";" ]; do sleep 1; done',
        "deny",
        "an escaped quote before a quoted-operator loop with no comparison bounds nothing",
    ),
    (
        'while [ "`echo "a -lt " x`" ]; do sleep 1; done',
        "deny",
        "a comparison inside a backtick that opens within double quotes bounds nothing",
    ),
    (
        "echo \\; ; while [ x = $'\\' ';' -lt $'\\' ] ; true; do sleep 1; done",
        "deny",
        "a `;` after a `$'` string that a backslash does not close is a separator",
    ),
    (
        'while [ "x"`true -lt 5` ]; do sleep 1; done',
        "deny",
        "a comparison inside a backtick glued to a quoted word bounds nothing",
    ),
    (
        'echo \\; ; while [ -f y ";" # -lt\ntrue; do sleep 1; done',
        "deny",
        "a comparison in a comment after an escape bounds nothing",
    ),
    (
        'echo \\; ; bash <<EOF\nuntil [ -f y \\\\"b" ";" \\\\"c" -lt 5 ]; do sleep 1; done\nEOF',
        "deny",
        "a quoted `;` in a heredoc a shell reads twice is a separator once the heredoc unescapes it",
    ),
    (
        'echo \\; ; bash -c "until [ -f y \\`\\";\\" -lt 5 \\` ]; do sleep 1; done"',
        "deny",
        "a comparison inside an escaped backtick in a `bash -c` payload bounds nothing",
    ),
    (
        "echo bash -c 'while true; do sleep 1; done'",
        "allow",
        "a shell named as an argument to something else runs no payload",
    ),
    (
        "> /tmp/log bash -c 'until [ -f x ]; do sleep 30; done'",
        "deny",
        "a leading redirection is not the run's command, and reading it as one skipped the payload",
    ),
    (
        "flock /tmp/l bash -c 'until [ -f x ]; do sleep 30; done'",
        "deny",
        "nor is a launcher this rule does not know by name, which is why the exemption is the set",
    ),
    (
        "xargs bash -c 'until [ -f x ]; do sleep 30; done'",
        "deny",
        "and the same for one that reads its arguments from a pipe",
    ),
    (
        "while true; do env -i sleep 30; done",
        "deny",
        "a launcher's own options sit between it and the sleep it runs",
    ),
    (
        "yes | while read line; do sleep 30; done",
        "deny",
        "a pipe's producer is unknown from the command text, so a read on one is no bound",
    ),
    (
        "while read l; do sleep 30; done < <(yes)",
        "deny",
        "and a process substitution is that same producer behind a redirect",
    ),
    (
        "yes | while read line; do sleep 30; done 2<errors",
        "deny",
        "a redirect on another descriptor leaves the read consuming the pipe on descriptor 0",
    ),
    (
        "while read l; do sleep 1; done 0< f",
        "allow",
        "while an explicit descriptor 0 is the one a read consumes",
    ),
    (
        "timeout 600 bash -c \"bash -c 'while true; do sleep 30; done' &\"",
        "deny",
        "an inherited timeout does not survive a wrapper the payload backgrounds",
    ),
    (
        "timeout 600 bash -c \"bash -c 'while true; do sleep 30; done' &\necho later\"",
        "deny",
        "and the tokenizer fuses that ampersand with the newline after it, which equality missed",
    ),
    (
        "yes | while read l; do sleep 30; done; cat < f",
        "deny",
        "a redirect on a later command binds nothing this loop reads",
    ),
    (
        "if while read l; do sleep 30; done then echo x < f; fi",
        "deny",
        "nor does one after a reserved word, which ends the loop's command as a separator does",
    ),
    (
        "if true; then while read l; do sleep 30; done else echo x < f; fi",
        "deny",
        "and an `else` ends it the same way a `then` does",
    ),
    (
        "if true; then if true; then while read l; do sleep 30; done fi else echo x < f; fi",
        "deny",
        "even behind a closing word, which ends it too",
    ),
    (
        "{ yes | while read l; do sleep 30; done } < f",
        "deny",
        "since a redirect after a closing word binds a compound whose pipe can still feed the loop",
    ),
    (
        "yes | while read l; do sleep 30; done # < f",
        "deny",
        "nor does one inside a trailing comment, which bash reads none of",
    ),
    (
        "yes | while read l; do sleep 30; done #< f",
        "deny",
        "and a comment glued to the redirect hides it the same way",
    ),
    (
        "yes | while read l; do sleep 30; done < #f",
        "deny",
        "a target opening a comment leaves the redirect with none",
    ),
    (
        "bash -c 'yes | while read l; do sleep 30; done # < f'",
        "deny",
        "and a payload's trailing comment hides its redirect as well",
    ),
    (
        "while read l; do sleep 30; done < f # x",
        "allow",
        "while a comment after the redirect leaves the file it binds in place",
    ),
    (
        "while read l; do sleep 30; done < '#f'",
        "allow",
        "and a quoted `#` names a file rather than opening a comment",
    ),
    (
        "while read l; do sleep 30; done < ${g:- #x} < /dev/zero",
        "deny",
        "a `#` inside an expansion opens no comment, so the stream after it still binds",
    ),
    (
        "while read l; do sleep 30; done < f < '#x' < /dev/zero\n# don't",
        "deny",
        "nor does one whose quoting is unknown end the scan before the stream that binds",
    ),
    (
        "while read l; do sleep 30; done < f > log\r#x < /dev/zero",
        "deny",
        "and a `#` after a carriage return sits inside a word bash reads whole",
    ),
    (
        "yes | while read l; do sleep 30; done {fd}< f",
        "deny",
        "nor does a descriptor named by a variable, which is never descriptor 0",
    ),
    (
        "yes | while read l; do sleep 30; done 00< in.txt",
        "allow",
        "while a padded zero is descriptor 0, which bash resolves as a number",
    ),
    (
        "while read l; do sleep 30; done " + "0" * 4400 + "< /dev/zero",
        "deny",
        "a zero-padded descriptor past int()'s digit limit is descriptor 0 and binds the stream",
    ),
    (
        "yes | while read l; do sleep 30; done " + "0" * 4400 + "< in.txt",
        "allow",
        "and binds a file just as the bare form does, rather than reading as another descriptor",
    ),
    (
        '< f cat "$(echo "\'")" ; (true)#\'\nyes | while read l ; do sleep 30 ; done #\' >',
        "deny",
        "a dangling redirect never takes the next separator as its target and reads on to a `< f`",
    ),
    (
        "yes | while read l; do sleep 30; done <",
        "deny",
        "a redirect with no target names no source, so it bounds nothing",
    ),
    (
        "while read l; do sleep 30; done < f",
        "allow",
        "while a redirect from a file names a source that ends",
    ),
    (
        "until read l; do sleep 30; done < f",
        "deny",
        "but an until loop over that same source never ends once the input is exhausted",
    ),
    (
        "timeout 600 bash -c '(while true; do sleep 30; done) &'",
        "deny",
        "a group closing between the loop and the ampersand backgrounds it just the same",
    ),
    (
        "timeout 600 bash -c '{ while true; do sleep 30; done; } &'",
        "deny",
        "and so does a brace group, whose closer arrives after the separator before it",
    ),
    (
        "timeout 600 bash -c '(while true; do sleep 30; done)&'",
        "deny",
        "and the tokenizer fuses that closer with the ampersand, which lstrip reads past",
    ),
    (
        "timeout 600 bash -c 'while true; do sleep 30; done & wait'",
        "deny",
        "a wait is no exception, since a disown or a reaped subshell empties it silently",
    ),
    (
        "timeout 600 bash -c 'while true; do sleep 30; done & disown; wait'",
        "deny",
        "which is the shape that proved the exception could not be verified from the text",
    ),
    (
        "timeout 600 bash -c '{ while true; do sleep 30; done; echo hi; } &'",
        "deny",
        "and a statement after the loop no longer hides the ampersand behind it",
    ),
    (
        "yes | while read l; do sleep 30; done < /dev/stdin",
        "deny",
        "a redirect from /dev/stdin re-opens the pipe the loop is already reading",
    ),
    (
        "yes | while read l; do sleep 30; done <&0",
        "deny",
        "and duplicating a descriptor rebinds that same pipe rather than opening a source",
    ),
    (
        "while read l; do sleep 30; done < /dev/zero",
        "deny",
        "and a stream that never reaches EOF ends no loop that reads it",
    ),
    (
        "yes | while read l; do sleep 30; done < /proc/self/fd/0",
        "deny",
        "which the /proc spelling of that same pipe does not escape",
    ),
    (
        "while read l; do sleep 30; done < /dev/full",
        "deny",
        "nor does a device left out of a list of the devices that never end",
    ),
    (
        "while read l; do sleep 30; done < /dev/./zero",
        "deny",
        "nor does a dot segment, since the target is normalized before it is read",
    ),
    (
        "timeout 600 bash -c 'sleep 1 & p=$!; while true; do sleep 30; done & wait $p'",
        "deny",
        "a wait naming one job returns when that job does, leaving the loop forked away",
    ),
    (
        "timeout 600 setsid --fork bash -c 'while true; do sleep 30; done'",
        "deny",
        "and setsid forks into a session no group signal from that timeout reaches",
    ),
    (
        "timeout 600 bash -c 'coproc { while true; do sleep 30; done; }'",
        "deny",
        "and coproc backgrounds with no operator for an ampersand scan to find",
    ),
    (
        "timeout 600 bash -c 'make build |& tee build.log; while true; do sleep 30; done'",
        "allow",
        "while the ampersand in a pipe-both operator backgrounds nothing",
    ),
    (
        "timeout 600 bash -c 'case $x in a) foo ;& b) bar ;; esac; while true; do sleep 30; done'",
        "allow",
        "nor does the one in a case arm that falls through to the next",
    ),
    (
        "timeout 600 bash -c 'setsid bash -c \"while true; do sleep 30; done\" & wait'",
        "deny",
        "which holds wherever the setsid is written, since each payload is read on its own terms",
    ),
    (
        "while read l; do sleep 30; done < //dev/zero",
        "deny",
        "a doubled leading slash is kept by POSIX and collapsed here before the tree is read",
    ),
    (
        "while read l; do sleep 30; done < in.txt < /dev/zero",
        "deny",
        "and the last redirect is the one descriptor 0 ends up bound to",
    ),
    (
        "while read -u 3 l; do sleep 30; done < f 3< /dev/zero",
        "deny",
        "while a read naming its own descriptor never draws on the one the redirect bounds",
    ),
    (
        "while read l; do sleep 30; done 2>&1 < in.txt",
        "allow",
        "while an earlier redirect's target is not this one's descriptor",
    ),
    (
        "while read l; do sleep 30; done > 2 < in.txt",
        "allow",
        "which holds when that target is itself a digit",
    ),
    (
        "while read l; do sleep 1; done \u00b2< f",
        "allow",
        "and a superscript digit reaches a verdict rather than raising, which failed every rule open",
    ),
    (
        "while true; do grep -i sleep f; done",
        "allow",
        "while a command that merely names sleep runs none",
    ),
    (
        "sudo -u ci bash -c 'until [ -f x ]; do sleep 30; done'",
        "deny",
        "while a prefix runs what follows it, whatever options sit between",
    ),
    (
        "timeout 0 bash -c 'until [ -f x ]; do sleep 30; done'",
        "deny",
        "whether a wrapper runs is a separate question from whether it is bounded",
    ),
    (
        "end=$((SECONDS + 600))\nwhile (( SECONDS < end )) && ! [ -f x ]; do sleep 30; done\nif (( SECONDS < end )); then echo MET; else echo 'NOT MET after 600s'; fi",
        "allow",
        "the arithmetic guard the denial itself names, which must not deny in turn",
    ),
    (
        "if timeout 600 bash -c 'until [ -f x ]; do sleep 30; done'; then echo MET; else echo 'NOT MET'; fi\n",
        "allow",
        "the timeout wrapper stays accepted wherever a single-command wait fits it",
    ),
    (
        'while read -r line; do echo "$line"; done < f',
        "allow",
        "a loop that does not sleep is no wait",
    ),
    (
        "for i in $(seq 1 60); do sleep 5; done",
        "allow",
        "a for loop is bounded by its own word list",
    ),
    ("sleep 30", "allow", "a sleep outside a loop ends on its own"),
    (
        'echo "while true; do sleep 1; done"',
        "allow",
        "the shape named inside a quoted value is text, not a loop",
    ),
    (
        "cat > d.md <<'EOF'\nuntil [ -f x ]; do sleep 5; done\nEOF",
        "allow",
        "a heredoc body written to a file is data, which is how this rule gets documented at all",
    ),
]


_CONTEXT_LEX_CASES = [
    (
        "echo \"$(date)\" 'x\nwhile true\n' # it's",
        ["echo", "$(date)", "x\nwhile true\n"],
        "a quote carries across lines",
    ),
    (
        "[[ $x =~ a|#b ]] # it's",
        ["[[", "$x", "=~", "a|#b", "]]"],
        "a `=~` operand holds `|` and `#`",
    ),
    (
        "x; [[ $x =~ a|#b ]] # it's",
        ["x", ";", "[[", "$x", "=~", "a|#b", "]]"],
        "a `[[` after a separator opens a conditional",
    ),
    (
        "[[ $x =~ (a b|c) ]]; y # it's",
        ["[[", "$x", "=~", "(a b|c)", "]]", ";", "y"],
        "a `=~` operand holds spaces inside parens",
    ),
    (
        "[[ ( $x =~ a ) ]] # it's",
        ["[[", "(", "$x", "=~", "a", ")", "]]"],
        "a `)` outside the operand's parens ends it",
    ),
    ("echo ${x#a} # it's", ["echo", "${x#a}"], "a parameter expansion is read whole"),
    ("(( x = 16#ff )) # it's", ["((", "x", "=", "16#ff", "))"], "a `#` inside arithmetic is text"),
    ("echo @(a|#b) # it's", ["echo", "@(a|#b)"], "an extglob pattern is one word"),
    ("!(x) # it's", ["!", "(", "x", ")"], "a `!(` opening a word is a negated subshell"),
    (
        'echo "$(echo "\'")" # it\'s',
        ["echo", '$(echo "\'")'],
        "a substitution's quotes start over inside it",
    ),
    (
        "echo \"$(x # it's\n)\" y # it's",
        ["echo", "$(x # it's\n)", "y"],
        "a comment inside a substitution hides its `)`",
    ),
    (
        "cat <<-EOF\n\tit's\n\tEOF\necho 'a\nb' # it's",
        ["cat", "<<", "-EOF", "\n", "it's", "\n", "EOF", "\n", "echo", "a\nb"],
        "a heredoc body line is read alone, and the dash form's tabbed delimiter ends the body",
    ),
    (
        "cat <<EOF\n;it's\nEOF\necho y # it's",
        ["cat", "<<", "EOF", "\n", ";it's", "\n", "EOF", "\n", "echo", "y"],
        "a heredoc body starts at the newline, even where its first line opens with an operator",
    ),
    (
        "(( ((a)) << 2 ))\necho 'x\ny'",
        ["((", "((", "a", "))", "<<", "2", "))\n", "echo", "x\ny"],
        "a paren group inside arithmetic does not end it, so a shift there opens no heredoc",
    ),
    (
        "cat << -\n#x\n-\necho y # it's",
        ["cat", "<<", "-", "\n", "#x", "\n", "-", "\n", "echo", "y"],
        "a spaced `-` is the delimiter, and a `#` in a body is text",
    ),
    ("echo \\#x 'a' # it's", ["echo", "#x", "a"], "an escaped `#` opens no comment"),
    ("echo $'it\\'s' # it's the PR's", ["echo", "it\\'s"], "a `$'...'` span honors its escapes"),
    (
        "echo `a # it's` b # it's",
        ["echo", "`a # it's`", "b"],
        "a backquote substitution is read whole",
    ),
    ('echo "a\\"b" # it\'s', ["echo", 'a"b'], "an escaped double quote does not end the span"),
    (
        "echo 'a' #it's\n#x\necho b",
        ["echo", "a", "\n", "\n", "echo", "b"],
        "a comment ends at its newline",
    ),
    (
        "echo \"$(echo $$'\\')\" y # it's",
        ["echo", "$(echo $$'\\')", "y"],
        "a `$$` inside a substitution opens no `$'` span",
    ),
    (
        'echo "$(( 1<<a\n))"\nz\na\necho y # it\'s',
        ["echo", "$(( 1<<a\n))", "\n", "z", "\n", "a", "\n", "echo", "y"],
        "a shift inside quoted arithmetic opens no heredoc",
    ),
    ("echo 'x", None, "an unterminated quote raises"),
    ('echo "$(x"', None, "an unterminated substitution raises"),
]


def _fixture_lookup(table):
    """A lookup over a fixture table keyed by POSIX-spelled directories, matching a key and a
    queried directory with every `\\` read as `/`. On Windows the rule's own relative join runs
    through `ntpath.normpath`, which rewrites `/` to `\\`, so an exact-string lookup misses there.
    Only the separator is folded, so collapsing `..` stays the rule's job and a case still fails
    where the rule stops doing it.
    """
    folded = {_fixture_dir(k): v for k, v in table.items()}
    return lambda d: folded.get(_fixture_dir(d))


def _fixture_dir(key):
    """Fold the separators in a fixture key, which is a directory or a `(directory, ref)` pair."""
    if isinstance(key, tuple):
        return (_fixture_dir(key[0]), *key[1:])
    return key.replace("\\", "/") if key else key


def _selftest():
    # A deterministic offline run, pinning origin to ptr727/PlexCleaner, the incident repo, so the cross-origin case resolves without touching a real checkout.
    # The gh-write cases inject empty rules and a feature current-branch so no case reaches the live branch-rules query.
    origin = ("ptr727", "plexcleaner")
    ok = True
    # Every existing loop below pins primary_checkout_lookup to a constant False (never a primary checkout), so rule 6 stays inert for every case that predates it.
    # Without this, a mutating subcommand incidental to a case testing a different rule (git commit, in a few of them) would fall through to the real _is_primary_checkout and resolve against wherever the self-test process actually runs, which is a primary checkout in CI, silently changing what those cases test.
    for cmd, want, label in _CASES:
        got, _ = classify(
            cmd,
            origin=origin,
            current_branch="feature/x",
            rules_lookup=lambda br: set(),
            environ={},
            primary_checkout_lookup=lambda d: False,
        )
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [{got:5}] want={want:5} {label}")
    for cmd, want, label in _WAIT_CASES:
        got, _ = classify(
            cmd,
            origin=origin,
            current_branch="feature/x",
            rules_lookup=lambda br: set(),
            environ={},
            primary_checkout_lookup=lambda d: False,
        )
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [{got:5}] want={want:5} {label}")
    for cmd, env, want, label in _SCOPE_CASES + _SCOPE_CASES_MORE:
        got, _ = classify(
            cmd,
            origin=origin,
            current_branch="feature/x",
            rules_lookup=lambda br: set(),
            environ=env,
            primary_checkout_lookup=lambda d: False,
        )
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [{got:5}] want={want:5} {label}")
    for cmd, env, want, label in _REPLY_RESOLVE_CASES:
        got, _ = classify(
            cmd,
            origin=origin,
            current_branch="feature/x",
            rules_lookup=lambda br: set(),
            environ=env,
            primary_checkout_lookup=lambda d: False,
        )
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [{got:5}] want={want:5} {label}")
    for cmd, cur, rmap, want, label in _GIT_CASES:
        got, _ = classify(
            cmd,
            origin=origin,
            current_branch=cur,
            rules_lookup=lambda br, _m=rmap: _m.get(br),
            environ={},
            primary_checkout_lookup=lambda d: False,
        )
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [{got:5}] want={want:5} {label}")
    for cmd, cwd, pmap, refmap, want, label in _PRIMARY_CHECKOUT_CASES:
        # A None refmap means every ref-check resolves True (an ordinary branch name).
        # A real map defaults a pair it does not name to True too, since only the pathspec-disambiguation cases care about a False answer.
        if refmap is None:
            ref_resolver = lambda _d, _r: True
        else:
            lookup = _fixture_lookup(refmap)
            ref_resolver = lambda d, r, _f=lookup: _f((d, r)) is not False
        got, _ = classify(
            cmd,
            cwd=cwd,
            origin=origin,
            current_branch="feature/x",
            rules_lookup=lambda br: set(),
            environ={},
            primary_checkout_lookup=_fixture_lookup(pmap),
            ref_resolver=ref_resolver,
            # No persisted alias resolves for any of these cases; only the dedicated alias table below exercises `_config_alias`.
            config_lookup=lambda _d, _n: None,
        )
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [{got:5}] want={want:5} {label}")
    # Git-alias resolution (requirement 6, finding 11): (cmd, cwd, pmap, config_map, want, label).
    # `config_map` stands in for the target checkout's own persisted `alias.<name>` config; an inline `-c alias.<name>=...` on the command itself needs no seam, it is read straight from the command text.
    for cmd, cwd, pmap, config_map, want, label in (
        (
            "git -c alias.wipe='reset --hard' wipe",
            "/primary",
            {"/primary": True},
            {},
            "deny",
            "an inline alias expanding to reset --hard is exactly as denied as spelling reset --hard out directly",
        ),
        (
            "git wipe",
            "/primary",
            {"/primary": True},
            {"wipe": "reset --hard"},
            "deny",
            "a persisted (non-inline) alias in the target checkout's own config resolves the same way",
        ),
        (
            "git wipe",
            "/worktree",
            {"/worktree": False},
            {"wipe": "reset --hard"},
            "allow",
            "the same alias in a linked worktree is allowed, rule 6 stays inert there regardless of alias resolution",
        ),
        (
            "git -c alias.wipe='!rm -rf .' wipe",
            "/primary",
            {"/primary": True},
            {},
            "deny",
            "a !-prefixed shell alias is opaque and denied conservatively rather than executed or allowed through",
        ),
        (
            "git peek",
            "/primary",
            {"/primary": True},
            {"peek": "status"},
            "allow",
            "an alias expanding to a read-only builtin is allowed, exactly as the builtin itself would be",
        ),
        (
            "git wipe",
            "/primary",
            {"/primary": True},
            {"wipe": "alsowipe", "alsowipe": "reset --hard"},
            "deny",
            "a chained alias (wipe -> alsowipe -> reset --hard) is followed through more than one hop",
        ),
        (
            "git nonexistent-alias",
            "/primary",
            {"/primary": True},
            {},
            "allow",
            "a subcommand this rule does not recognize and that resolves to no alias at all falls through to allow, matching the rule's stance for any other unrecognized subcommand",
        ),
        (
            'git -c alias.wipe="reset --hard \'unterminated" wipe',
            "/primary",
            {"/primary": True},
            {},
            "allow",
            (
                "a malformed alias expansion (unbalanced quotes) fails to parse as shell words; "
                "this is treated as unresolvable rather than raising ValueError and crashing the "
                "hook on a config value neither the agent nor this rule controls"
            ),
        ),
    ):
        got, _ = classify(
            cmd,
            cwd=cwd,
            origin=origin,
            current_branch="feature/x",
            rules_lookup=lambda br: set(),
            environ={},
            primary_checkout_lookup=_fixture_lookup(pmap),
            config_lookup=lambda _d, n, _m=config_map: _m.get(n),
        )
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [{got:5}] want={want:5} {label}")
    # The GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT escape hatch, checked once here rather than folded into the table above, since these are the cases needing a non-empty environ alongside a primary_checkout_lookup.
    # "git reset --hard", not a flagless checkout, since checkout is exempt anyway and would pass identically with the grant check deleted, the exact vacuous-test gap a review caught here.
    for env, want, label in (
        (
            {"GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT": "1"},
            "allow",
            "the escape hatch allows even a denied shape when granted",
        ),
        (
            {"GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT": "0"},
            "deny",
            "a value of 0 reads as not granted, not as any-non-empty-string-is-truthy",
        ),
        (
            {"GH_WRITE_GUARD_ALLOW_PRIMARY_CHECKOUT": "false"},
            "deny",
            "a value of false reads as not granted either",
        ),
        ({}, "deny", "no grant at all is the ordinary denied case"),
    ):
        got, _ = classify(
            "git reset --hard",
            cwd="/primary",
            origin=origin,
            current_branch="feature/x",
            rules_lookup=lambda br: set(),
            environ=env,
            primary_checkout_lookup=_fixture_lookup({"/primary": True}),
        )
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [{got:5}] want={want:5} {label}")
    global _MSYS_DRIVE_PATHS
    saved_msys = _MSYS_DRIVE_PATHS
    _MSYS_DRIVE_PATHS = True
    try:
        for cmd, cwd, want, label in (
            (
                "git reset --hard",
                "/c/repos/primary",
                "deny",
                "a Git Bash drive spelling of the hook's cwd",
            ),
            (
                "git -C primary reset --hard",
                "/c/repos",
                "deny",
                "a relative -C joined onto a Git Bash drive cwd",
            ),
            (
                "git -C /c/repos/primary reset --hard",
                None,
                "deny",
                "a Git Bash drive spelling in -C",
            ),
            (
                "cd /c/repos/primary && git reset --hard",
                None,
                "deny",
                "a Git Bash drive spelling in a leading cd",
            ),
            (
                "git --work-tree=/c/repos/primary reset --hard",
                None,
                "deny",
                "a Git Bash drive spelling in --work-tree",
            ),
            (
                "GIT_WORK_TREE=/c/repos/primary git reset --hard",
                None,
                "deny",
                "a Git Bash drive spelling in GIT_WORK_TREE=",
            ),
            (
                "git --git-dir=/c/repos/primary/.git reset --hard",
                None,
                "deny",
                "a Git Bash drive spelling in --git-dir",
            ),
            (
                "GIT_DIR=/c/repos/primary/.git git reset --hard",
                None,
                "deny",
                "a Git Bash drive spelling in GIT_DIR=",
            ),
            (
                "git -C /c/repos/worktree reset --hard",
                None,
                "allow",
                "a Git Bash drive spelling of a linked worktree",
            ),
        ):
            got, _ = classify(
                cmd,
                cwd=cwd,
                origin=origin,
                current_branch="feature/x",
                rules_lookup=lambda br: set(),
                environ={},
                primary_checkout_lookup=_fixture_lookup(
                    {
                        "C:/repos/primary": True,
                        "C:/repos/primary/.git": True,
                        "C:/repos/worktree": False,
                    }
                ),
            )
            mark = "ok  " if got == want else "FAIL"
            if got != want:
                ok = False
            print(f"  {mark} [{got:5}] want={want:5} {label}")
        for raw, want in (
            ("/c", "C:/"),
            ("/d/x", "D:/x"),
            ("/primary", "/primary"),
            ("//c/x", "//c/x"),
            ("c/x", "c/x"),
        ):
            got = _msys_drive_path(raw)
            mark = "ok  " if got == want else "FAIL"
            if got != want:
                ok = False
            print(f"  {mark} [msys ] {raw} reads as {want}")
    finally:
        _MSYS_DRIVE_PATHS = saved_msys
    for cmd, want, label in _CONTEXT_LEX_CASES:
        try:
            got = _context_lex(cmd)
        except ValueError:
            got = None
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [lex  ] {label}")
    for label, cmd in (
        ("a long run of `((` pairs", "(" * 20000 + "x" + " )" * 20000),
        ("`((` in many heredoc bodies", "(( 1 ))\ncat <<E\n((a\n((b\n((c\n((d\n((e\nE\n" * 4000),
    ):
        start = time.monotonic()
        _context_lex(cmd)
        elapsed = time.monotonic() - start
        mark = "ok  " if elapsed < 5 else "FAIL"
        if elapsed >= 5:
            ok = False
        print(f"  {mark} [lex  ] {label} is scanned in linear time ({elapsed:.2f}s)")
    for label, read in (
        (
            "a wait-loop scan of 200 chained evals",
            lambda: _unbounded_wait_loop("eval " * 200 + "echo sleep"),
        ),
        (
            "a redirected wait-loop scan of 200 chained evals",
            lambda: _unbounded_wait_loop("eval " * 200 + "echo sleep > f"),
        ),
        (
            "a sleep scan of 200 chained evals",
            lambda: _sleeps(_shell_tokens("eval " * 200 + "echo x > f")),
        ),
        (
            "an unknown-quoting wait-loop scan of 200 chained evals",
            lambda: _unbounded_wait_loop("eval " * 200 + 'x\necho "$(echo "it\'s")"'),
        ),
        (
            "an unknown-quoting wait-loop scan of 200 evals whose payloads stay unknown",
            lambda: _unbounded_wait_loop('eval "a\'b"; ' * 200 + '\necho "$(echo "it\'s")"'),
        ),
        (
            "an unknown-quoting sleep scan of a loop body of 200 evals whose payloads stay unknown",
            lambda: _unbounded_wait_loop(
                "while [ -f x ]; do " + 'eval "a\'b"; ' * 200 + 'done\necho "$(echo "it\'s")"'
            ),
        ),
        (
            "an unknown-quoting wait-loop scan of ten evals on a 50 KB line",
            lambda: _unbounded_wait_loop(
                'eval "a\'b"; ' * 10 + "y " * 25_000 + '\necho "$(echo "it\'s")"'
            ),
        ),
        (
            "a wait-loop scan of 4000 evals that are redirection targets",
            lambda: _unbounded_wait_loop(
                "echo " + ">eval " * 4000 + "; while true; do sleep 1; done"
            ),
        ),
        (
            "a wait-loop scan of 16000 evals that are echo's arguments",
            lambda: _unbounded_wait_loop("echo " + "eval " * 16000),
        ),
        (
            "an unknown-quoting sleep scan of a loop body of 30 evals that never sleeps",
            lambda: _unbounded_wait_loop(
                "while [ -f x ]; do " + "eval x; " * 30 + 'done\necho "$(echo "it\'s")"'
            ),
        ),
    ):
        start = time.monotonic()
        read()
        elapsed = time.monotonic() - start
        mark = "ok  " if elapsed < 5 else "FAIL"
        if elapsed >= 5:
            ok = False
        print(f"  {mark} [wait ] {label} is fast ({elapsed:.2f}s)")
    real_lex = _operator_lex
    quote_kept_lexes = []

    def counting_lex(text, posix=True):
        if not posix:
            quote_kept_lexes.append(text)
        return real_lex(text, posix)

    globals()["_operator_lex"] = counting_lex
    try:
        _unbounded_wait_loop("while [ -f x ]; do bash -c 'echo a'; done")
    finally:
        globals()["_operator_lex"] = real_lex
    lexed_once = len(quote_kept_lexes) == 1
    if not lexed_once:
        ok = False
    print(
        f"  {'ok  ' if lexed_once else 'FAIL'} [wait ] a loop's bash -c payload holding no eval "
        f"is lexed for its quoting once ({len(quote_kept_lexes)})"
    )
    # The one case that spawns git rather than stubbing it, since what it covers is the decode inside that spawn.
    # A checkout whose path is not UTF-8 decoded strictly raised, which read as unresolvable, and the guard then allowed a mutating command in a primary checkout it had failed to recognize.
    got = _is_primary_checkout_selftest()
    # "skip" is the setup failing, which is neither a pass nor a failure, and it is deliberately not None.
    # None is `_is_primary_checkout` answering unresolvable, which is the defect shape this case exists to catch, so sharing the skip channel with it would report that defect as a skip and pass.
    mark = "ok  " if got is True else ("skip" if got == "skip" else "FAIL")
    if got is not True and got != "skip":
        ok = False
    print(f"  {mark} [{got!s:5}] want=True  a checkout path that is not UTF-8 still resolves")
    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return 0 if ok else 1


def _is_primary_checkout_selftest():
    """Resolve a real primary checkout whose directory name is not valid UTF-8.

    "skip" where the case could not run at all, which is neither a pass nor a failure. Windows
    refuses such a name outright, a sandbox can refuse the directory, and a host can have no git,
    and reporting any of those as a pass would say the decode was exercised when it never ran.

    A string rather than None, since None is what `_is_primary_checkout` answers when git did not
    resolve, which is the defect this case exists to catch and must stay a failure.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        try:
            # The decode is inside the guard too, since Windows decodes a filesystem name with surrogatepass, which refuses this byte rather than carrying it the way Linux does.
            target = os.path.join(tmp, os.fsdecode(b"checkout_\xe9"))
            os.mkdir(target)
        except (OSError, UnicodeError):
            return "skip"
        # An ambient GIT_DIR points `git init` at that repository rather than at this temp one, and `_is_primary_checkout` then resolves both of its answers to it and reports a pass the decode never reached, leaving a repository behind outside the temp tree.
        # Cleared around both spawns rather than passed to either, since `_is_primary_checkout` reads the environment it inherits and honoring GIT_DIR is deliberate there.
        cleared = {}
        for name in (
            "GIT_DIR",
            "GIT_COMMON_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_CEILING_DIRECTORIES",
            "GIT_DISCOVERY_ACROSS_FILESYSTEM",
            "GIT_NAMESPACE",
            "GIT_PREFIX",
        ):
            if name in os.environ:
                cleared[name] = os.environ.pop(name)
        try:
            try:
                r = subprocess.run(["git", "init", "-q", target], capture_output=True, check=False)
            except OSError:
                return "skip"
            if r.returncode != 0:
                return "skip"
            return _is_primary_checkout(target)
        finally:
            os.environ.update(cleared)


# --- Hook entrypoint (PreToolUse) --------------------------------------------------------------------
def _main():
    try:
        data = json.load(sys.stdin)
    # The hook must never crash on input it does not recognize.
    except Exception:  # noqa: BLE001 - malformed JSON on stdin is exactly the "not our event shape" case this guards, not a defect to propagate as a hook-crashing traceback.
        sys.exit(0)  # not our event shape - do not interfere
    # Valid JSON that is not a dict (a bare string, number, or list), or a `tool_input`/`command` value of the wrong type, is the same "not our event shape" case the JSON-parse guard above already handles, confirmed to otherwise raise AttributeError/TypeError uncaught and exit non-zero rather than the documented deny/allow shape.
    # A non-zero exit is a hook error, not a decision, and a PreToolUse hook erroring lets the tool call proceed exactly as if this hook had allowed it, so failing open here on a malformed shape matches what an uncaught crash would do anyway, deliberately rather than by accident.
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        sys.exit(0)
    tool_input = data.get("tool_input")
    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(cmd, str):
        cmd = ""
    cwd = data.get("cwd") or os.getcwd()
    if not isinstance(cwd, str):
        cwd = os.getcwd()
    decision, reason = classify(cmd, cwd)
    if decision == "deny":
        # Documented PreToolUse deny contract (confirm field names against current docs before shipping).
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
        sys.exit(0)
    sys.exit(0)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    _main()

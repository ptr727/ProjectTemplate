#!/usr/bin/env python3
"""PreToolUse guard: deny the GitHub-write footguns behind the cross-repo comment incident.

Registered as a Claude Code PreToolUse hook on the Bash tool. It reads the tool-input JSON on stdin,
classifies the command, and DENIES (with a reason shown to the agent) when a command is a GitHub *write*
matching a known-dangerous pattern. Reads and everything that is not a clear write pass through.

Precision over recall for the write-footgun shapes (1-3): they deny the specific shapes that caused the
incident, not everything unparseable - a false deny would break the agent, and a miss still falls under
the GOVERNANCE.md "Repository Boundaries and Write Safety" prose rules. The branch-bypass rule (4) instead
fails CLOSED on the protected-by-default branches, because the harm there is a silent success under the
maintainer's admin bypass. The denied shapes:

  1. a state-changing gh call whose output is discarded or forced to success
     (>/dev/null, 2>/dev/null, &>/dev/null, || true, || :, || echo)
  2. a GraphQL mutation passing a literal GitHub node id (PRRT_/PR_/BOT_/...) instead of a $variable
  3. a gh write with an explicit -R/--repo/repos/<owner>/<repo> target under an owner other than the
     checkout origin's, unless the maintainer granted that target in GH_WRITE_GUARD_ALLOW. Sibling
     repositories under the same owner are allowed, since the harm this guards is reaching a stranger's
     repository, not working across your own fleet in one session.
  4. a git operation that would only land by bypassing an active branch rule: a direct push to a branch
     whose rules require a pull request, a force-push where history is protected, a delete where deletion
     is blocked, or an explicit-bypass flag (`gh pr merge --admin`, `git commit/push --no-verify`). The
     branch's live rules are the judge, so a code-style develop is denied and a config-style develop is
     allowed with no hardcoded repo list.
  5. a hand-rolled reply/resolve for a review thread: a `resolveReviewThread` mutation via `gh api
     graphql`, or a POST to the review-comment replies endpoint, where `scripts/pr_review.py reply ...
     --resolve` is the documented one-call path. Splitting the two into separate hand-run acts is what
     let a reply sit unresolved across a push and a re-request, reading as untriaged to a maintainer
     skimming the pull request (the incident behind this rule). Permitted only under the same
     GH_WRITE_GUARD_ALLOW grant rule 3 reads, since the helper refuses a cross-owner pull request outright
     and the hand-run GraphQL form is then the documented fallback, not a footgun.

Run `gh-write-guard.py --selftest` to verify the decision matrix without Claude Code.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from urllib.parse import quote

# --- What counts as a GitHub write -------------------------------------------------------------------
# The gh subcommands that mutate.
# The `gh api` command is handled separately, since it needs field and method inspection.
_GH_WRITE_SUB = re.compile(
    r"""\bgh\s+(?:
        pr\s+(?:create|comment|close|merge|edit|review|reopen|ready|lock|unlock)
      | issue\s+(?:create|comment|close|edit|reopen|delete|lock|unlock|pin|unpin|transfer)
      | release\s+(?:create|edit|delete|upload)
      | repo\s+(?:create|delete|edit|rename|archive)
      | (?:label|secret|variable|ruleset)\s+(?:create|delete|edit|set)
      | gist\s+(?:create|edit|delete)
    )\b""",
    re.VERBOSE,
)
_GH_API = re.compile(r"\bgh\s+api\b")
# `-X`/`--method` accept both a separate value (`-X POST`) and an attached one (`-XPOST`,
# `--method=POST`), so this must match either spelling, matching how `_gh_effective_method` reads it.
_EXPLICIT_WRITE_METHOD = re.compile(
    r"(?:--method[= ]|-X)\s*(?:POST|PUT|PATCH|DELETE)\b", re.IGNORECASE
)
# A gh api call with a field flag defaults to POST even without -X, so it is a write.
# `-f`/`-F` also accept an attached value (`-fbody=x`), so no trailing `\b` is required after them.
# It is required after the long-form spellings, where one legitimately separates the flag from the next word.
_API_FIELD_FLAG = re.compile(r"(?:^|\s)(?:-f|-F)|(?:^|\s)(?:--field|--raw-field|--input)\b")
_GRAPHQL = re.compile(r"\bgh\s+api\b.*\bgraphql\b", re.DOTALL)
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
_GH_ADMIN_MERGE = re.compile(r"\bgh\s+pr\s+merge\b[^\n|&;]*(?:^|\s)--admin\b")

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
_REPOS_PATH_TOKEN = re.compile(r"^repos/(?P<owner>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+)")
# Flags whose own value is opaque text (a PR/issue title or body, a GraphQL field, a jq/template expression, a header), and so is skipped whole rather than pattern-matched for a repo target.
# Without this, a --body describing a `--repo <owner>/<repo>` doc line, or a commit message quoting the same convention, reads as a real flag.
# The incident this closes denied an ordinary `git commit` whose message body merely quoted the fleet's own `--repo owner/repo` example text.
_GH_TEXT_VALUE_FLAGS = {
    "--title",
    "-t",
    "--body",
    "-b",
    "--body-file",
    "--notes",
    "--notes-file",
    "--message",
    "-m",
    "--desc",
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
    if _GH_WRITE_SUB.search(cmd) or _push_arg_lists(cmd):
        return True
    if _GH_API.search(cmd):
        if _EXPLICIT_WRITE_METHOD.search(cmd):
            return True
        if _GRAPHQL.search(cmd) and _MUTATION.search(cmd):
            return True
        if _API_FIELD_FLAG.search(cmd) and not _GRAPHQL.search(cmd):
            return True  # gh api <path> -f k=v  => POST
    return False


def _origin_owner_repo(cwd):
    try:
        url = subprocess.run(
            ["git", "-C", cwd or ".", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
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


def _shell_tokens(cmd):
    """Tokenize like a shell, isolating operator runs (`|`, `&&`, `;`, newline, `>`, `2>&1`, ...) as
    their own tokens even when glued to a word - so a `>` or a newline inside a quoted value stays part
    of that token while a real redirection or line break is separated. Degrades gracefully if the
    quoting cannot be parsed.
    """
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=_PUNCTUATION_CHARS)
        lex.whitespace_split = True
        lex.whitespace = lex.whitespace.replace(
            "\n", ""
        )  # A newline is an operator above rather than a gap between words.
        return list(lex)
    except (ValueError, TypeError):  # bad quoting, or punctuation_chars unsupported on old Python
        # Neither fallback isolates an operator, so the lines are split here to keep the one thing this path must not lose, that a newline ends the command before it.
        toks = []
        for i, line in enumerate(cmd.split("\n")):
            if i:
                toks.append("\n")
            try:
                toks.extend(shlex.split(line, posix=True))
            except ValueError:
                toks.extend(line.split())
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
            out.append(args)
            i = k
        else:
            i += 1  # this `git` was a different subcommand; keep scanning
    return out


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
        n = len(args)
        i = 0
        while i < n:
            t = args[i]
            if t in _GH_TEXT_VALUE_FLAGS and "=" not in t:
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
            m = _REPOS_PATH_TOKEN.match(t)
            if m and "<" not in t:
                targets.append((m.group("owner").lower(), m.group("repo").lower()))
            i += 1
    return targets


def _gh_api_path(args):
    """The positional API path argument of a `gh api <path> ...` invocation's own argv, or None. Skips
    the invocation's own value-taking flags first (`-X POST`, `-f k=v`, ...) so their values are never
    mistaken for the path positional.
    """
    if not args or args[0] != "api":
        return None
    n = len(args)
    i = 1
    while i < n:
        t = args[i]
        if t in _GH_TEXT_VALUE_FLAGS and "=" not in t:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t
    return None


def _gh_field_value(tok):
    """The `name=value` field text carried by one token, in every field-flag spelling `gh` accepts: a
    bare `-f`/`-F`/`--field`/`--raw-field` (the caller reads the next token as the value), the
    equals-attached long form (`--field=name=value`/`--raw-field=name=value`), or the attached short form
    (`-fname=value`/`-Fname=value`, no separator). Returns None for a bare flag, whose value is the next
    token rather than part of this one.
    """
    for pfx in ("--field=", "--raw-field="):
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


def _gh_effective_method(args):
    """The effective HTTP method of a `gh api` invocation's own argv: an explicit `-X`/`--method` value
    when present, in every spelling `gh` accepts, else POST when a field flag is present (`gh`'s own
    default for a write-shaped call), else GET.
    """
    n = len(args)
    i = 0
    method = None
    has_field = False
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
            method = t[2:].upper()
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


def classify(cmd, cwd=None, origin=None, current_branch=None, rules_lookup=None, environ=None):
    """Return (decision, reason). decision is 'allow' or 'deny'.

    origin, when given, is a (owner, repo) tuple used instead of resolving from cwd - the self-test
    passes it for a deterministic, offline run. current_branch, rules_lookup and environ are likewise
    test seams: current_branch stands in for the git resolution of a bare push, rules_lookup(branch)
    stands in for the live branch-rules query, and environ stands in for the process environment the
    maintainer's grant is read from.
    """
    # Fold shell line-continuations so a multi-line Bash invocation, such as `gh pr merge 5 \<newline> --admin`, parses as one command.
    # Only backslash-newline is joined, so a real newline between commands still separates them.
    cmd = re.sub(r"\\\r?\n", " ", cmd)
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


def _selftest():
    # A deterministic offline run, pinning origin to ptr727/PlexCleaner, the incident repo, so the cross-origin case resolves without touching a real checkout.
    # The gh-write cases inject empty rules and a feature current-branch so no case reaches the live branch-rules query.
    origin = ("ptr727", "plexcleaner")
    ok = True
    for cmd, want, label in _CASES:
        got, _ = classify(
            cmd,
            origin=origin,
            current_branch="feature/x",
            rules_lookup=lambda br: set(),
            environ={},
        )
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [{got:5}] want={want:5} {label}")
    for cmd, env, want, label in _SCOPE_CASES:
        got, _ = classify(
            cmd,
            origin=origin,
            current_branch="feature/x",
            rules_lookup=lambda br: set(),
            environ=env,
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
        )
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [{got:5}] want={want:5} {label}")
    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return 0 if ok else 1


# --- Hook entrypoint (PreToolUse) --------------------------------------------------------------------
def _main():
    try:
        data = json.load(sys.stdin)
    # The hook must never crash on input it does not recognize.
    except Exception:  # noqa: BLE001
        sys.exit(0)  # not our event shape - do not interfere
    if data.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = (data.get("tool_input") or {}).get("command", "")
    cwd = data.get("cwd") or os.getcwd()
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

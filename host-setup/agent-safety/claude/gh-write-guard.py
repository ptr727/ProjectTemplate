#!/usr/bin/env python3
"""PreToolUse guard: deny the GitHub-write footguns and the primary-checkout mutation behind two incidents.

Registered as a Claude Code PreToolUse hook on the Bash tool. It reads the tool-input JSON on stdin,
classifies the command, and DENIES (with a reason shown to the agent) when a command is a GitHub *write*
matching a known-dangerous pattern, or a mutating git operation run directly against a primary checkout.
Reads and everything that is not a clear write pass through. See host-setup/agent-safety/README.md for
the requirements this implements, stated once, agent-agnostic, and for how to audit this file against
them.

Precision over recall for the write-footgun shapes (1-3) and the primary-checkout shape (6): they deny
the specific shapes that caused an incident, not everything unparseable, since a false deny would break the
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

Run `gh-write-guard.py --selftest` to verify the decision matrix without Claude Code.
"""

import json
import os
import re
import shlex
import subprocess
import sys
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
        # `shlex.shlex`'s own default keeps `#` as a comment starter, unlike `shlex.split()`, which explicitly clears it, and confirmed live to otherwise fuse `git fetch origin # x\ngit reset --hard` into one invocation, hiding the second command from every tokenizer-based rule.
        # Cleared unconditionally: a truncated command is a far worse failure than an ordinary `#` becoming literal trailing argv words instead.
        lex.commenters = ""
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
    return _HOME_VAR_RE.sub(lambda _m: os.environ.get("HOME", ""), value)


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
            ref_resolver = lambda d, r, _m=refmap: _m.get((d, r), True)
        got, _ = classify(
            cmd,
            cwd=cwd,
            origin=origin,
            current_branch="feature/x",
            rules_lookup=lambda br: set(),
            environ={},
            primary_checkout_lookup=lambda d, _m=pmap: _m.get(d),
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
            primary_checkout_lookup=lambda d, _m=pmap: _m.get(d),
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
            primary_checkout_lookup=lambda d: {"/primary": True}.get(d),
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

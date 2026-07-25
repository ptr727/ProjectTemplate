#!/usr/bin/env python3
"""PreToolUse guard: deny the GitHub-write footguns behind the cross-repo comment incident.

Registered as a Claude Code PreToolUse hook on the Bash tool. It reads the tool-input JSON on stdin,
classifies the command, and DENIES (with a reason shown to the agent) when a command is a GitHub *write*
matching a known-dangerous pattern. Reads and everything that is not a clear write pass through.

Precision over recall for the write-footgun shapes (1-3): they deny the specific shapes that caused the
incident, not everything unparseable - a false deny would break the agent, and a miss still falls under
the AGENTS.md "Repository Boundaries and Write Safety" prose rules. The branch-bypass rule (4) instead
fails CLOSED on the protected-by-default branches, because the harm there is a silent success under the
maintainer's admin bypass. The denied shapes:

  1. a state-changing gh call whose output is discarded or forced to success
     (>/dev/null, 2>/dev/null, &>/dev/null, || true, || :, || echo)
  2. a GraphQL mutation passing a literal GitHub node id (PRRT_/PR_/BOT_/...) instead of a $variable
  3. a gh write with an explicit -R/--repo/repos/<owner>/<repo> target outside the checkout's origin
  4. a git operation that would only land by bypassing an active branch rule: a direct push to a branch
     whose rules require a pull request, a force-push where history is protected, a delete where deletion
     is blocked, or an explicit-bypass flag (`gh pr merge --admin`, `git commit/push --no-verify`). The
     branch's live rules are the judge, so a code-style develop is denied and a config-style develop is
     allowed with no hardcoded repo list.

Run `gh-write-guard.py --selftest` to verify the decision matrix without Claude Code.
"""
import json
import os
import re
import subprocess
import sys

# --- What counts as a GitHub write -------------------------------------------------------------------
# gh subcommands that mutate. `gh api` is handled separately (it needs field/method inspection).
_GH_WRITE_SUB = re.compile(
    r"""\bgh\s+(?:
        pr\s+(?:create|comment|close|merge|edit|review|reopen|ready|lock|unlock)
      | issue\s+(?:create|comment|close|edit|reopen|delete|lock|unlock|pin|unpin|transfer)
      | release\s+(?:create|edit|delete|upload)
      | repo\s+(?:create|delete|edit|rename|archive)
      | (?:label|secret|variable|ruleset)\s+(?:create|delete|edit|set)
      | gist\s+(?:create|edit|delete)
    )\b""",
    re.X,
)
_GH_API = re.compile(r"\bgh\s+api\b")
_EXPLICIT_WRITE_METHOD = re.compile(r"(?:--method|-X)\s+(?:POST|PUT|PATCH|DELETE)\b", re.I)
# gh api with a field flag defaults to POST even without -X, so it is a write.
_API_FIELD_FLAG = re.compile(r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)\b")
_GRAPHQL = re.compile(r"\bgh\s+api\b.*\bgraphql\b", re.S)
_MUTATION = re.compile(r"\bmutation\b")
_GIT_PUSH = re.compile(r"\bgit\s+push\b")
_GIT_COMMIT = re.compile(r"\bgit\s+commit\b")

# --- Bypass-of-branch-rule detectors (Rule 4) --------------------------------------------------------
# A git operation is denied when it would only succeed by bypassing an active branch rule - the harm is
# that the maintainer's admin identity CAN bypass, so a plain-looking push silently lands on a protected
# branch. The judgement is made against the branch's *live* rules (self-configuring: a code-style develop
# carries `pull_request` and is denied, a config-style develop does not and is allowed), except for the
# explicit-bypass flags below, which are the bypass by definition and need no query.
#
# Branches that fail CLOSED when their rules cannot be read - protected-by-default across every config.
_PROTECTED_DEFAULT = {"main", "master", "develop"}
# `gh pr merge --admin` overrides required reviews/status checks with admin power.
_GH_ADMIN_MERGE = re.compile(r"\bgh\s+pr\s+merge\b[^\n|&;]*(?:^|\s)--admin\b")
# `--no-verify` skips the local git hooks (signing / lint / pre-push gates). `git commit` also spells it
# `-n`; `git push -n` means --dry-run, so the short form is a bypass only for commit.
_NO_VERIFY_LONG = re.compile(r"(?:^|\s)--no-verify\b")
_COMMIT_SHORT_N = re.compile(r"(?:^|\s)-n\b")

# --- Risk-pattern detectors --------------------------------------------------------------------------
# Output-discard / force-success tails. Bare `2>&1` is NOT here: it merges stderr into stdout, leaving
# the output visible, so it is not suppression (and denying it would break `... 2>&1 | tee log`).
_SUPPRESS = re.compile(r">\s*/dev/null|&>\s*/dev/null|2>\s*/dev/null|\|\|\s*(?:true\b|echo\b|:)")
# A quoted argument value ("..." or '...'). Stripped before the suppression scan so a --body/--title
# that merely mentions `|| true` or `>/dev/null` as text is not mistaken for a real command tail. Real
# suppression tails are unquoted shell operators, so stripping quotes never hides an actual footgun. The
# double-quoted form allows `\"` escapes so an embedded quote does not end the span early; shell single
# quotes take no escapes, so their form is literal.
_QUOTED_SPAN = re.compile(r'"(?:\\.|[^"\\])*"' r"|'[^']*'")
# A GitHub global node id literal: an UPPERCASE prefix (PR_, PRRT_, IC_, BOT_, ...) + a long base64url
# body, or a legacy MD... base64 id. The uppercase prefix plus a >=12-char body keeps it from matching
# an ordinary underscored word in a reply body (e.g. body="fixed_the_thing_now", lowercase prefix).
_NODE_ID_LITERAL = re.compile(r'^(?:[A-Z]{1,5}_[A-Za-z0-9_\-]{12,}|MD[A-Za-z0-9]{12,})$')
# -F/-f name=VALUE, capturing the value - handles "quoted" and bare
_FIELD_ASSIGN = re.compile(r"""(?:-F|-f|--field|--raw-field)\s+[A-Za-z_][\w]*=(?P<v>'[^']*'|"[^"]*"|\S+)""")
_EXPLICIT_REPO = re.compile(r"(?:-R|--repo)\s+(?P<q>['\"]?)(?P<r>[^\s'\"]+)(?P=q)")
_API_REPO_PATH = re.compile(r"\bgh\s+api\b[^\n|]*?\brepos/(?P<owner>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+)")


def _is_gh_write(cmd):
    if _GH_WRITE_SUB.search(cmd) or _GIT_PUSH.search(cmd):
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
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return None
    m = re.search(r"[:/]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$", url)
    return (m.group(1).lower(), m.group(2).lower()) if m else None


def _live_branch_rules(owner, repo, branch):
    """Return the set of active rule types on a branch, or None if the query cannot be resolved.

    None (not an empty set) signals "unknown" so the caller can fail closed on a protected-default
    branch. An empty set means the branch genuinely has no rules (a feature branch).
    """
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/rules/branches/{branch}", "--jq", "[.[].type]"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        return set(json.loads(r.stdout or "[]"))
    except Exception:
        return None


def _current_push_branch(cwd):
    """Resolve the destination branch of a bare `git push` from the branch's configured push target."""
    for args in (
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}"],
        ["rev-parse", "--abbrev-ref", "HEAD"],
    ):
        try:
            r = subprocess.run(["git", "-C", cwd or ".", *args], capture_output=True, text=True, timeout=5)
        except Exception:
            return None
        ref = r.stdout.strip()
        if r.returncode == 0 and ref and ref != "HEAD":
            return ref.split("/", 1)[1] if "/" in ref else ref
    return None


# Flags that consume the following token as a value, so the value is not a positional (remote/refspec).
_PUSH_VALUE_FLAGS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def _push_targets(cmd, cwd=None, current_branch=None):
    """Parse a `git push` invocation into (op, [branches]).

    op is 'delete' | 'force' | 'update'. current_branch, when given, stands in for the git resolution
    of a bare push (the self-test passes it for an offline run).
    """
    m = _GIT_PUSH.search(cmd)
    seg = cmd[m.end():]
    seg = re.split(r"&&|\|\||[;|\n]", seg)[0]
    toks = seg.split()
    force = delete = False
    positionals = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if ">" in t or "<" in t:
            break  # a redirection operator (>, 2>, >>, <, 2>&1): end of git argv, start of shell syntax
        if t in ("--force", "-f") or t.startswith("--force-with-lease"):
            force = True
        elif t in ("--delete", "-d"):
            delete = True
        elif t in _PUSH_VALUE_FLAGS:
            i += 1  # skip this flag's value
        elif t.startswith("-"):
            pass  # some other flag (e.g. -u, --tags, --no-verify)
        else:
            positionals.append(t)
        i += 1
    # positionals are [remote, refspec...]; a lone positional is the remote (a bare push).
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
            dst = dst[len("refs/heads/"):]
        elif dst.startswith("refs/"):
            continue  # a tag or other non-branch ref
        if dst:
            branches.append(dst)
    if not refspecs and not delete:
        b = current_branch if current_branch is not None else _current_push_branch(cwd)
        if b:
            branches = [b]
    op = "delete" if delete else ("force" if force else "update")
    return op, branches


def _handoff(cmd):
    return (
        " The agent must not bypass this - if the bypass is genuinely intended, hand the exact command "
        "to Pieter to run in his terminal. See AGENTS.md 'Repository Boundaries and Write Safety' and the "
        "Branching Model."
    )


def _check_bypass_flags(cmd):
    """Deny the explicit-bypass flags: they are a bypass by definition, no branch query needed."""
    bare = _QUOTED_SPAN.sub("", cmd)  # a flag mentioned inside a quoted message/body is not a real flag
    if _GH_ADMIN_MERGE.search(bare):
        return "deny", (
            "This uses `gh pr merge --admin`, which merges past required reviews and status checks using "
            "admin power - a bypass of the merge gate. Merge only when the gate is satisfied." + _handoff(cmd)
        )
    if _NO_VERIFY_LONG.search(bare) or (_GIT_COMMIT.search(bare) and _COMMIT_SHORT_N.search(bare)):
        return "deny", (
            "This uses --no-verify, which skips the git hooks (signing, lint, and pre-push gates). "
            "Skipping verification is a bypass; run the command without it." + _handoff(cmd)
        )
    return "allow", ""


def _check_push_bypass(cmd, cwd, origin, current_branch=None, rules_lookup=None):
    """Deny a git push that would only succeed by bypassing an active branch rule."""
    if origin is None:
        origin = _origin_owner_repo(cwd)
    op, branches = _push_targets(cmd, cwd, current_branch)
    for br in branches:
        rules = rules_lookup(br) if rules_lookup is not None else (
            _live_branch_rules(origin[0], origin[1], br) if origin else None
        )
        if rules is None:
            if br in _PROTECTED_DEFAULT:
                return "deny", (
                    f"Could not read the branch rules for '{br}', a protected-by-default branch "
                    f"(main/master/develop). Failing closed rather than risk a silent bypass - retry when "
                    f"the API is reachable." + _handoff(cmd)
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
                f"This deletes '{br}', whose rules forbid deletion (rule: deletion)." + _handoff(cmd)
            )
    return "allow", ""


def classify(cmd, cwd=None, origin=None, current_branch=None, rules_lookup=None):
    """Return (decision, reason). decision is 'allow' or 'deny'.

    origin, when given, is a (owner, repo) tuple used instead of resolving from cwd - the self-test
    passes it for a deterministic, offline run. current_branch and rules_lookup are likewise test seams:
    current_branch stands in for the git resolution of a bare push, and rules_lookup(branch) stands in
    for the live branch-rules query.
    """
    # Fold shell line-continuations so a multi-line Bash invocation (`gh pr merge 5 \<newline> --admin`)
    # parses as one command; only backslash-newline is joined, so a real newline between commands still
    # separates them.
    cmd = re.sub(r"\\\r?\n", " ", cmd)
    # Rule 4: a git operation that would only succeed by bypassing an active branch rule. Checked before
    # the gh-write gate below, since `git commit --no-verify` is a bypass yet not a GitHub write.
    dec, reason = _check_bypass_flags(cmd)
    if dec == "deny":
        return dec, reason
    if _GIT_PUSH.search(cmd):
        dec, reason = _check_push_bypass(cmd, cwd, origin, current_branch, rules_lookup)
        if dec == "deny":
            return dec, reason

    if not _is_gh_write(cmd):
        return "allow", ""

    # 1. suppressed output on a write - scan with quoted argument values removed so a --body/--title
    #    that only mentions a suppression token as text does not false-deny a legitimate write.
    if _SUPPRESS.search(_QUOTED_SPAN.sub("", cmd)):
        return "deny", (
            "This is a GitHub write with its output discarded or forced to success "
            "(>/dev/null, 2>/dev/null, &>/dev/null, || true, || :, || echo). "
            "A write's result is exactly what must be read: a mutation can succeed on the server "
            "while the client reports an error. Run it without the output-discarding tail and read "
            "the response. See AGENTS.md 'Repository Boundaries and Write Safety'."
        )

    # 2. literal node id in a mutation
    if _GRAPHQL.search(cmd) and _MUTATION.search(cmd):
        for m in _FIELD_ASSIGN.finditer(cmd):
            val = m.group("v").strip("'\"")
            if val.startswith("$") or val.startswith("${"):
                continue
            if _NODE_ID_LITERAL.match(val):
                return "deny", (
                    f"This mutation passes a literal GitHub node id ({val[:16]}...) instead of a "
                    "variable captured from a live query. Node ids resolve globally, so a fabricated "
                    "or stale id writes to a real object in another repository. Capture the id from a "
                    "query in this session into a variable and pass -F ...=\"$VAR\". See AGENTS.md "
                    "'Repository Boundaries and Write Safety'."
                )

    # 3. explicit target outside origin
    if origin is None:
        origin = _origin_owner_repo(cwd)
    targets = []
    mr = _EXPLICIT_REPO.search(cmd)
    if mr and "/" in mr.group("r") and "<" not in mr.group("r"):
        o, r = mr.group("r").split("/", 1)
        targets.append((o.lower(), r.lower()))
    for m in _API_REPO_PATH.finditer(cmd):
        if "<" not in m.group("owner"):
            targets.append((m.group("owner").lower(), m.group("repo").lower()))
    # Only runs when origin resolves (a git checkout): with no project context there is nothing to
    # compare an explicit target against, so this check is skipped and rules 1-2 still apply. A node-id
    # target is invisible here regardless - that is what rule 2 guards.
    if origin:
        for t in targets:
            if t != origin:
                return "deny", (
                    f"This write targets {t[0]}/{t[1]}, which is not this checkout's origin "
                    f"({origin[0]}/{origin[1]}). Write only to the current project's own repository. "
                    "Another repository needs explicit per-session permission. See AGENTS.md "
                    "'Repository Boundaries and Write Safety'."
                )

    return "allow", ""


# --- Self-test ---------------------------------------------------------------------------------------
_CASES = [
    # (command, expected_decision, label)
    ("gh api graphql -f query='mutation($t:ID!){addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$t,body:\"x\"}){comment{id}}}' -F t=\"PRRT_kwDODvuuzM6SFvx0\" >/dev/null 2>&1 || true", "deny", "the incident: suppressed + literal id"),
    ("gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"PRRT_kwDOabc123def\"", "deny", "literal node id in a mutation"),
    ("gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"$TID\"", "allow", "mutation with captured $TID"),
    ("gh issue comment 5 -R mankatcheung/job-finder --body \"hi\"", "deny", "cross-origin explicit -R"),
    ("gh issue comment 5 -R \"mankatcheung/job-finder\" --body \"hi\"", "deny", "cross-origin quoted -R"),
    ("gh pr create --title x --body y >/dev/null 2>&1", "deny", "suppressed gh pr create"),
    ("gh api repos/ptr727/PlexCleaner/issues/1/comments -f body=\"ok\"", "allow", "gh api POST to origin"),
    ("gh api graphql -f query='{repository(owner:\"o\",name:\"r\"){pullRequest(number:1){reviewThreads(first:100){nodes{id}}}}}'", "allow", "graphql READ query"),
    ("gh pr view 5 --json reviews", "allow", "gh pr view (read)"),
    ("return 1 2>/dev/null || exit 1", "allow", "shell guard, not a gh write"),
    ("git commit -m 'x' && git push >/dev/null 2>&1", "deny", "push with discarded output"),
    ("gh issue comment 5 --body x 2>&1 | tee out.log", "allow", "bare 2>&1 piped to tee is not suppression"),
    ("gh pr comment 5 --body ok 2>&1", "allow", "bare 2>&1 leaves output visible"),
    ("gh api repos/ptr727/PlexCleaner/issues/1/comments -f body=x 2>/dev/null", "deny", "stderr discarded on a write"),
    ("gh issue comment 5 --body \"run make || true to skip errors\"", "allow", "|| true inside a quoted body is not a tail"),
    ("gh pr comment 5 --body \"pipe noisy output to >/dev/null\"", "allow", ">/dev/null inside a quoted body is not a redirect"),
    ("gh issue comment 5 --body \"see notes\" >/dev/null", "deny", "real redirect after a quoted body still denies"),
    ("gh issue comment 5 --body \"he said \\\"pipe to >/dev/null\\\" today\"", "allow", "escaped quotes in a body do not end the span early"),
    ("gh pr close 5 || :", "deny", "force-success no-op tail on a write"),
    ("gh pr comment 5 --body x || echo done", "deny", "force-success echo tail on a write"),
    ("gh api graphql -f query='mutation{addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$t,body:$b}){comment{id}}}' -F t=\"$TID\" -F b=\"fixed_the_underscore_bug_here\"", "allow", "underscored reply body is not a node id"),
    ("gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"TODO_fixit\"", "allow", "short all-caps token is not a node id"),
]

# Rule-4 (branch-rule bypass) cases. Each carries its own branch->rules map so the run is deterministic
# and offline - the real hook queries the live rules, here rules_lookup is injected. current_branch
# stands in for the git resolution of a bare push. `None` rules mean the query could not be read.
_CODE_RULES = {"deletion", "non_fast_forward", "required_linear_history", "required_signatures",
               "pull_request", "required_status_checks", "copilot_code_review"}  # code-style develop / any main
_CONFIG_RULES = {"deletion", "non_fast_forward", "required_signatures"}  # config-style develop: no pull_request
_GIT_CASES = [
    # (command, current_branch, {branch: rules_set_or_None}, expected_decision, label)
    ("git push origin develop", None, {"develop": _CODE_RULES}, "deny", "code-style develop: direct push bypasses pull_request"),
    ("git push origin develop", None, {"develop": _CONFIG_RULES}, "allow", "config-style develop: no pull_request rule, direct push allowed"),
    ("git push origin main", None, {"main": _CODE_RULES}, "deny", "main: direct push bypasses pull_request"),
    ("git push origin feature/x", None, {"feature/x": set()}, "allow", "feature branch: no rules, allowed"),
    ("git push -u origin feature/x", None, {"feature/x": set()}, "allow", "feature branch with -u: allowed"),
    ("git push origin HEAD:develop", None, {"develop": _CODE_RULES}, "deny", "HEAD:develop refspec resolves to develop"),
    ("git push origin abc1234:refs/heads/main", None, {"main": _CODE_RULES}, "deny", "sha:refs/heads/main resolves to main"),
    ("git push", "develop", {"develop": _CODE_RULES}, "deny", "bare push resolving to develop"),
    ("git push", "feature/x", {"feature/x": set()}, "allow", "bare push resolving to a feature branch"),
    ("git push --force origin develop", None, {"develop": _CONFIG_RULES}, "deny", "force-push denied by non_fast_forward even on config develop"),
    ("git push --force-with-lease origin feature/x", None, {"feature/x": set()}, "allow", "force-with-lease to a ruleless feature branch"),
    ("git push origin +HEAD:develop", None, {"develop": _CODE_RULES}, "deny", "+refspec is a force-push to develop"),
    ("git push --delete origin develop", None, {"develop": _CODE_RULES}, "deny", "delete develop denied by deletion rule"),
    ("git push origin :develop", None, {"develop": _CODE_RULES}, "deny", "empty-source :develop is a delete"),
    ("git push origin develop", None, {"develop": None}, "deny", "develop with unreadable rules: fail closed"),
    ("git push origin feature/x", None, {"feature/x": None}, "allow", "feature branch with unreadable rules: fail open"),
    ("git commit --no-verify -m x", None, {}, "deny", "commit --no-verify skips the hooks"),
    ("git commit -n -m x", None, {}, "deny", "commit -n is --no-verify"),
    ("git push --no-verify origin feature/x", None, {"feature/x": set()}, "deny", "push --no-verify is a bypass even on a feature branch"),
    ("git push -n origin develop", None, {"develop": _CODE_RULES}, "deny", "push -n is dry-run not no-verify, but the direct push to develop still denies"),
    ("gh pr merge 5 --admin --squash", None, {}, "deny", "gh pr merge --admin overrides the merge gate"),
    ("gh pr merge 5 \\\n  --admin --squash", None, {}, "deny", "line-continued gh pr merge --admin still caught"),
    ("git commit -m 'mention --no-verify in the message'", None, {}, "allow", "--no-verify inside a quoted message is not a flag"),
    ("git push >push.log 2>&1", "develop", {"develop": _CODE_RULES}, "deny", "redirection tokens are not a branch: bare push to develop still denies"),
    ("git push origin develop >push.log 2>&1", None, {"develop": _CODE_RULES}, "deny", "redirect after a real refspec does not hide the develop target"),
]


def _selftest():
    # Deterministic offline run: pin origin to ptr727/PlexCleaner (the incident repo) so the cross-origin
    # case resolves without touching a real checkout. The gh-write cases inject empty rules + a feature
    # current-branch so no case reaches the live branch-rules query.
    origin = ("ptr727", "plexcleaner")
    ok = True
    for cmd, want, label in _CASES:
        got, _ = classify(cmd, origin=origin, current_branch="feature/x", rules_lookup=lambda br: set())
        mark = "ok  " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mark} [{got:5}] want={want:5} {label}")
    for cmd, cur, rmap, want, label in _GIT_CASES:
        got, _ = classify(cmd, origin=origin, current_branch=cur, rules_lookup=lambda br, _m=rmap: _m.get(br))
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
    except Exception:
        sys.exit(0)  # not our event shape - do not interfere
    if data.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = (data.get("tool_input") or {}).get("command", "")
    cwd = data.get("cwd") or os.getcwd()
    decision, reason = classify(cmd, cwd)
    if decision == "deny":
        # Documented PreToolUse deny contract (confirm field names against current docs before shipping).
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))
        sys.exit(0)
    sys.exit(0)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    _main()

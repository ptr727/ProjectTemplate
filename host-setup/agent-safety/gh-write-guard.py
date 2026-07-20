#!/usr/bin/env python3
"""PreToolUse guard: deny the GitHub-write footguns behind the cross-repo comment incident.

Registered as a Claude Code PreToolUse hook on the Bash tool. It reads the tool-input JSON on stdin,
classifies the command, and DENIES (with a reason shown to the agent) when a command is a GitHub *write*
matching a known-dangerous pattern. Reads and everything that is not a clear write pass through.

Precision over recall by design: it denies the specific shapes that caused the incident, not everything
it cannot parse. A false deny would break the agent, while a missed case still falls under the AGENTS.md
"Repository Boundaries and Write Safety" prose rules. The three denied shapes:

  1. a state-changing gh call whose output is discarded (>/dev/null, &>/dev/null, 2>/dev/null, || true)
  2. a GraphQL mutation passing a literal GitHub node id (PRRT_/PR_/BOT_/...) instead of a $variable
  3. a gh write with an explicit -R/--repo/repos/<owner>/<repo> target outside the checkout's origin

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

# --- Risk-pattern detectors --------------------------------------------------------------------------
# Output-discard / force-success tails. Bare `2>&1` is NOT here: it merges stderr into stdout, leaving
# the output visible, so it is not suppression (and denying it would break `... 2>&1 | tee log`).
_SUPPRESS = re.compile(r">\s*/dev/null|&>\s*/dev/null|2>\s*/dev/null|\|\|\s*(?:true|:|echo)\b")
# A GitHub global node id literal: an UPPERCASE prefix (PR_, PRRT_, IC_, BOT_, ...) + a long base64url
# body, or a legacy MD... base64 id. The uppercase prefix plus a >=12-char body keeps it from matching
# an ordinary underscored word in a reply body (e.g. body="fixed_the_thing_now", lowercase prefix).
_NODE_ID_LITERAL = re.compile(r'^(?:[A-Z]{1,5}_[A-Za-z0-9_\-]{12,}|MD[A-Za-z0-9]{12,})$')
# -F/-f name=VALUE, capturing the value - handles "quoted" and bare
_FIELD_ASSIGN = re.compile(r"""(?:-F|-f|--field|--raw-field)\s+[A-Za-z_][\w]*=(?P<v>'[^']*'|"[^"]*"|\S+)""")
_EXPLICIT_REPO = re.compile(r"(?:-R|--repo)\s+(?P<r>[^\s'\"]+)")
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


def classify(cmd, cwd=None, origin=None):
    """Return (decision, reason). decision is 'allow' or 'deny'.

    origin, when given, is a (owner, repo) tuple used instead of resolving from cwd - the self-test
    passes it for a deterministic, offline run.
    """
    if not _is_gh_write(cmd):
        return "allow", ""

    # 1. suppressed output on a write
    if _SUPPRESS.search(cmd):
        return "deny", (
            "This is a GitHub write with its output discarded (>/dev/null, &>/dev/null, || true). "
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
    ("gh pr create --title x --body y >/dev/null 2>&1", "deny", "suppressed gh pr create"),
    ("gh api repos/ptr727/PlexCleaner/issues/1/comments -f body=\"ok\"", "allow", "gh api POST to origin"),
    ("gh api graphql -f query='{repository(owner:\"o\",name:\"r\"){pullRequest(number:1){reviewThreads(first:100){nodes{id}}}}}'", "allow", "graphql READ query"),
    ("gh pr view 5 --json reviews", "allow", "gh pr view (read)"),
    ("return 1 2>/dev/null || exit 1", "allow", "shell guard, not a gh write"),
    ("git push origin develop", "allow", "normal push (no suppression, no cross-repo)"),
    ("git commit -m 'x' && git push >/dev/null 2>&1", "deny", "push with discarded output"),
    ("gh issue comment 5 --body x 2>&1 | tee out.log", "allow", "bare 2>&1 piped to tee is not suppression"),
    ("gh pr comment 5 --body ok 2>&1", "allow", "bare 2>&1 leaves output visible"),
    ("gh api repos/ptr727/PlexCleaner/issues/1/comments -f body=x 2>/dev/null", "deny", "stderr discarded on a write"),
    ("gh api graphql -f query='mutation{addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$t,body:$b}){comment{id}}}' -F t=\"$TID\" -F b=\"fixed_the_underscore_bug_here\"", "allow", "underscored reply body is not a node id"),
    ("gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F t=\"TODO_fixit\"", "allow", "short all-caps token is not a node id"),
]


def _selftest():
    # Deterministic offline run: pin origin to ptr727/PlexCleaner (the incident repo) so the
    # cross-origin case resolves without touching a real checkout.
    origin = ("ptr727", "plexcleaner")
    ok = True
    for cmd, want, label in _CASES:
        got, _ = classify(cmd, origin=origin)
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

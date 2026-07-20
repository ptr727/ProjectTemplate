#!/usr/bin/env python3
"""Live fleet audit: the deterministic subset of AUDIT.md, run from the hub (stdlib + gh).

Compares each cataloged registry repo against the ground truth in this repo - general settings
(repo-config/settings.json), branch rulesets (normalized diff vs the model's payloads), secret
names (spec/secrets.json; values are never read), baseline/per-type file presence on the
ground-truth branch (spec/files.json), and branch-model facts (main/develop existence, develop
behind main). Owner-initiated: run it when onboarding a repo, when drift is suspected, or before
fleet-wide changes. Read-only - it never modifies a target.

Findings: DEFECT (an applicable check fails outright), LETTER (a required file is absent - intent
unverified, judge per AUDIT.md section 7), DRIFT (non-breaking divergence, e.g. main-side
changes develop lacks, a stale secret, a registry field contradicting reality), ERROR (a gh call
failed, so the repo could not be fully audited). Exits non-zero when any repo has a DEFECT,
LETTER, or ERROR finding.

Usage: python3 spec/audit.py [RepoName ...]   (default: every cataloged repo)
"""
import base64
import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent

SETTINGS_KEYS = [
    "has_wiki", "has_projects", "allow_merge_commit", "allow_squash_merge",
    "allow_rebase_merge", "allow_auto_merge", "allow_update_branch", "delete_branch_on_merge",
]
RULESET_SUBSET = ["name", "target", "enforcement", "bypass_actors", "conditions", "rules"]
# Phrases in a registry driftNote that assert work still outstanding. Deliberately specific: a note
# recording a permanent deviation ("no get-version-task; relies on validate-task") must not match.
PENDING_MARKERS = ["pending", "not yet", "owed", "todo", "still", "behind", "missing", "absent"]


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def hub_name():
    """This repo's name on the remote, and whether it came from the remote.

    Read from origin rather than the checkout directory, which a differently-named clone, a fork, or a
    worktree without an origin would silently break. The caller announces the directory-name fallback:
    a silently degraded match is the fail-open case this check exists to prevent.
    """
    r = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True, text=True, cwd=ROOT)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rstrip("/").removesuffix(".git").split("/")[-1], True
    return ROOT.name, False


HUB_NAME, HUB_NAME_FROM_REMOTE = hub_name()


def gh(path, ok404=False):
    """GET a REST path via gh; parsed JSON, or None on 404 when ok404.

    No --paginate: on object endpoints it concatenates page documents into unparseable JSON. Every
    list read here fits one page; callers pass per_page=100 where a default page could truncate.
    """
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if r.returncode != 0:
        if ok404 and ("HTTP 404" in r.stderr or "Not Found" in r.stderr):
            return None
        raise RuntimeError(f"gh api {path}: {r.stderr.strip().splitlines()[-1] if r.stderr else 'failed'}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def normalize_ruleset(payload):
    sub = {k: payload.get(k) for k in RULESET_SUBSET}
    if isinstance(sub.get("rules"), list):
        sub["rules"] = sorted(sub["rules"], key=lambda r: json.dumps(r, sort_keys=True))
    if isinstance(sub.get("bypass_actors"), list):
        sub["bypass_actors"] = sorted(sub["bypass_actors"], key=lambda a: (str(a.get("actor_type")), str(a.get("actor_id"))))
    return json.dumps(sub, sort_keys=True)


def repo_slug(entry):
    # url is https://github.com/<owner>/<repo>
    return "/".join(entry["url"].rstrip("/").split("/")[-2:])


def repo_selectors(entry, defaults):
    """The scope-selector set a files.json appliesTo is matched against (see spec/scope-model.md).

    The four namespaces - project types, workflowModel, releaseTrigger, consumerModel - are disjoint, so
    a flat token set is unambiguous. Defaults resolve the same way configure.sh does (repo -> defaults ->
    fleet default), so a repo relying on a defaults value scopes the same as one setting it explicitly.
    """
    sel = set(entry.get("types", []))
    sel.add(entry.get("workflowModel") or defaults.get("workflowModel") or "release")
    sel.add(entry.get("releaseTrigger") or defaults.get("releaseTrigger") or "two-phase")
    cm = entry.get("consumerModel") or defaults.get("consumerModel")
    if cm:
        sel.add(cm)
    return sel


def applies(applies_to, sel):
    """True if an appliesTo selector applies to a repo's selector set. Disjunctive (any-of); `*` is all."""
    if applies_to == "*":
        return True
    tokens = applies_to if isinstance(applies_to, list) else [applies_to]
    return bool(set(tokens) & sel)


def audit_repo(entry, spec):
    findings = []  # (kind, text)
    slug = repo_slug(entry)
    types = entry.get("types", [])
    model = entry.get("workflowModel") or spec["registry"].get("defaults", {}).get("workflowModel") or "release"
    ground = entry.get("groundTruthBranch", "main")

    try:
        live = gh(f"repos/{slug}")
    except RuntimeError as e:
        return [("ERROR", str(e))], ""

    # --- Branch facts ---
    branch_main = gh(f"repos/{slug}/branches/main", ok404=True)
    branch_dev = gh(f"repos/{slug}/branches/develop", ok404=True)
    main_exists, dev_exists = branch_main is not None, branch_dev is not None
    if not main_exists:
        findings.append(("DEFECT", "branch: main does not exist"))
    if bool(entry.get("hasDevelop")) != dev_exists:
        findings.append(("DRIFT", f"registry: hasDevelop={entry.get('hasDevelop')} but develop {'exists' if dev_exists else 'is absent'}"))
    if main_exists and dev_exists:
        # Commit counts mislead here: merge-commit promotions leave main permanently "ahead" while the
        # head trees are identical, so tree equality is the no-drift fast path. When the head trees
        # differ, empty compare files[] means develop is merely ahead (no main-side changes since the
        # merge-base) - normal, no finding, no further API calls.
        if branch_main["commit"]["commit"]["tree"]["sha"] != branch_dev["commit"]["commit"]["tree"]["sha"]:
            cmp = gh(f"repos/{slug}/compare/develop...main", ok404=True)
            if cmp and cmp.get("files"):
                # Non-empty files[] signals main-side changes, but is not usable directly: it is blind
                # to cherry-picked promotions (develop may already hold identical content under
                # different commit SHAs, e.g. promote/* branches) AND capped at 300 entries (#336).
                # Instead, derive the main-side change set from the merge-base tree - paths whose
                # object SHA (blob, or submodule pointer) differs base->main, additions and deletions
                # included, no cap - then drop paths whose objects already match at develop: content
                # develop already has is not "content develop lacks". Three recursive trees calls; if
                # any tree is truncated (or unexpectedly not a dict) the filter is skipped and the
                # compare's unfiltered count kept (conservative, marked).
                trees = {
                    "base": gh(f"repos/{slug}/git/trees/{cmp['merge_base_commit']['commit']['tree']['sha']}?recursive=1"),
                    "develop": gh(f"repos/{slug}/git/trees/{branch_dev['commit']['commit']['tree']['sha']}?recursive=1"),
                    "main": gh(f"repos/{slug}/git/trees/{branch_main['commit']['commit']['tree']['sha']}?recursive=1"),
                }
                if not all(isinstance(t, dict) for t in trees.values()) or any(t.get("truncated") for t in trees.values()):
                    findings.append(("DRIFT", f"branch: {len(cmp['files'])}+ main-side path change(s) develop lacks (forward-sync needed; tree unavailable or too large to filter cherry-pick noise)"))
                else:
                    objs = {name: {e["path"]: e["sha"] for e in t["tree"] if e["type"] in ("blob", "commit")} for name, t in trees.items()}
                    changed_on_main = {p for p in set(objs["base"]) | set(objs["main"]) if objs["base"].get(p) != objs["main"].get(p)}
                    lacking = sorted(p for p in changed_on_main if objs["main"].get(p) != objs["develop"].get(p))
                    if lacking:
                        shown = ", ".join(lacking[:8]) + (" ..." if len(lacking) > 8 else "")
                        findings.append(("DRIFT", f"branch: {len(lacking)} main-side path change(s) develop lacks (forward-sync needed): {shown}"))

    # --- General settings ---
    expected = dict(spec["settings"])
    expected["has_discussions"] = not live.get("private", True)
    for key in SETTINGS_KEYS + ["has_discussions"]:
        if key in expected and live.get(key) != expected[key]:
            findings.append(("DEFECT", f"settings: {key} live={live.get(key)} expected={expected[key]}"))
    if main_exists and live.get("default_branch") != "main":
        findings.append(("DEFECT", f"settings: default_branch is {live.get('default_branch')}, expected main"))

    # --- Rulesets ---
    dev_payload = "repo-config/operational/develop.json" if model == "operational" else "repo-config/develop.json"
    expect_rulesets = {"develop": load(dev_payload), "main": load("repo-config/main.json")}
    # No ok404: a repo with no rulesets returns an empty list, so a 404 means the call failed
    # (access/rename) and must surface as ERROR, not read as "no rulesets".
    live_list = gh(f"repos/{slug}/rulesets?per_page=100") or []
    live_names = [r["name"] for r in live_list]
    for name, payload in expect_rulesets.items():
        ids = [r["id"] for r in live_list if r["name"] == name]
        if not ids:
            findings.append(("DEFECT", f"ruleset: {name} missing"))
            continue
        if len(ids) > 1:
            findings.append(("DRIFT", f"ruleset: {len(ids)} rulesets named {name} (resolve the duplicates)"))
        live_rs = gh(f"repos/{slug}/rulesets/{ids[0]}")
        if normalize_ruleset(live_rs) != normalize_ruleset(payload):
            findings.append(("DEFECT", f"ruleset: {name} diverges from {dev_payload if name == 'develop' else 'repo-config/main.json'} (normalized diff)"))
    for stray in [n for n in live_names if n not in expect_rulesets]:
        findings.append(("DRIFT", f"ruleset: stray ruleset '{stray}'"))

    # --- Secrets (names only) ---
    secrets = spec["secrets"]
    stores = {}
    # No ok404: an empty store returns {"secrets": []}, so a 404/403 (permissions, rename) must
    # surface as ERROR rather than cascade into false missing-secret DEFECTs.
    for store, path in [("actions", f"repos/{slug}/actions/secrets?per_page=100"), ("dependabot", f"repos/{slug}/dependabot/secrets?per_page=100")]:
        data = gh(path)
        stores[store] = {s["name"] for s in (data or {}).get("secrets", [])}
    mechanisms = [secrets["targetMechanisms"].get(p.get("target")) for p in entry.get("publish", [])]
    mechanisms += [secrets.get("typeMechanisms", {}).get(t) for t in types]
    claimed = [secrets["mechanisms"][m] for m in mechanisms if m and m in secrets["mechanisms"]]
    required_by_store = {"actions": set(), "dependabot": set()}
    for store in secrets["baseline"].get("stores", []):
        required_by_store[store] |= set(secrets["baseline"].get("requires", []))
    for mech in claimed:
        for store in mech.get("stores", []):
            required_by_store[store] |= set(mech.get("requires", []))
    # Registry requiredSecrets[] are the domain-specific additions (STANDUP.md: requiredSecrets plus the
    # implicit baseline). Mechanism-mapped names already carry their stores above; unmapped ones are
    # expected in the actions store and count as claimed (never stale).
    required_by_store["actions"] |= set(entry.get("requiredSecrets", []))
    forbidden = set(secrets["baseline"].get("forbids", []))
    for mech in claimed:
        forbidden |= set(mech.get("forbids", []))
    for store, required in required_by_store.items():
        for name in sorted(required - stores[store]):
            findings.append(("DEFECT", f"secrets: {name} missing from the {store} store"))
    for store, present in stores.items():
        for name in sorted(present & forbidden):
            findings.append(("DEFECT", f"secrets: forbidden {name} present in the {store} store"))
        claimed_names = required_by_store["actions"] | required_by_store["dependabot"]
        for name in sorted(present - claimed_names):
            findings.append(("DRIFT", f"secrets: {name} in the {store} store is claimed by no applicable mechanism (stale?)"))

    # --- Carried files must not reference the template repo ---
    # The template is private, so a reference 404s for this repo's users and exposes machinery they cannot
    # follow. Checks the agent-instruction files, where a stale "report drift upstream" paragraph spread.
    for path in ("AGENTS.md", ".github/copilot-instructions.md"):
        doc = gh(f"repos/{slug}/contents/{path}?ref={ground}", ok404=True)
        if doc and doc.get("content") and HUB_NAME.lower() in base64.b64decode(doc["content"]).decode("utf-8", "replace").lower():
            findings.append(("DRIFT", f"carried: {path} references the template repo by name or link (private - 404s for this repo's readers; state the behavior, not the destination)"))

    # --- Dependabot ecosystem coverage ---
    # A repo's tree implies Dependabot ecosystems it must track: github-actions when it ships workflows
    # (the action versions they reference otherwise go stale, and a merge-bot then has no PRs to auto-merge),
    # devcontainers when it ships a .devcontainer. dependabot.yml is YAML (no stdlib parser), so scan the
    # declared package-ecosystem values by regex - anchored to the line start so a commented-out entry
    # (# package-ecosystem: ...) is not read as declared. This asserts an implied ecosystem's *presence*
    # only; that each declared ecosystem dual-targets main+develop (the fleet norm) is verified by
    # inspection, not here. Only runs when dependabot.yml exists; its absence is already a file-presence
    # LETTER below. Language ecosystems (nuget/uv/npm) are directory-scoped and not yet cross-checked here.
    db = gh(f"repos/{slug}/contents/.github/dependabot.yml?ref={ground}", ok404=True)
    if db and db.get("content"):
        declared = set(re.findall(r'^[ \t]*-?[ \t]*package-ecosystem:[ \t]*["\']?([\w-]+)', base64.b64decode(db["content"]).decode("utf-8", "replace"), re.M))
        implied = {}
        workflows = gh(f"repos/{slug}/contents/.github/workflows?ref={ground}", ok404=True)
        if isinstance(workflows, list) and any(e["name"].endswith((".yml", ".yaml")) for e in workflows):
            implied["github-actions"] = ".github/workflows/ is present"
        if gh(f"repos/{slug}/contents/.devcontainer?ref={ground}", ok404=True) is not None:
            implied["devcontainers"] = ".devcontainer/ is present"
        for eco, why in sorted(implied.items()):
            if eco not in declared:
                findings.append(("DRIFT", f"dependabot: {eco} ecosystem not declared though {why}; add it for both main and develop per the fleet norm"))

    # --- File presence on the ground-truth branch ---
    # appliesTo is matched against the repo's full selector set (types + workflowModel + releaseTrigger +
    # consumerModel), so the release/operational develop ruleset is two data entries, not a code swap.
    sel = repo_selectors(entry, spec["registry"].get("defaults", {}))
    seen_paths = set()
    for item in spec["files"]["baseline"]:
        if not applies(item.get("appliesTo", "*"), sel):
            continue
        path = item["path"]
        if path in seen_paths:
            continue
        seen_paths.add(path)
        if gh(f"repos/{slug}/contents/{path}?ref={ground}", ok404=True) is None:
            findings.append(("LETTER", f"file: {path} absent on {ground} (verify intent per AUDIT.md section 7)"))

    # --- Registry driftNotes freshness ---
    # Gated on everything else passing: a clean repo has no outstanding work for a pending-marker note to
    # describe. Narrow markers keep a permanent-deviation note ("relies on validate-task") from tripping.
    if not findings:
        for note in entry.get("driftNotes", []):
            marker = next((w for w in PENDING_MARKERS if re.search(rf"\b{re.escape(w)}\b", note, re.I)), None)
            if marker:
                findings.append(("DRIFT", f"registry: driftNote says '{marker}' but the audit is clean - verify and reconcile: \"{note[:70]}{'...' if len(note) > 70 else ''}\""))

    # Stamp the commit actually read for the ground-truth branch. Never fall back to the other branch:
    # a stamp naming develop while carrying main's sha would misattribute every finding.
    ground_branch = branch_dev if ground == "develop" else branch_main
    audited_sha = (ground_branch or {}).get("commit", {}).get("sha", "")
    return findings, audited_sha


def main():
    spec = {
        "registry": load("registry/repos.json"),
        "settings": load("repo-config/settings.json"),
        "secrets": load("spec/secrets.json"),
        "files": load("spec/files.json"),
    }
    wanted = {n.lower() for n in sys.argv[1:]}
    repos = [r for r in spec["registry"]["repos"] if r.get("status") == "cataloged"]
    if wanted:
        repos = [r for r in repos if r["name"].lower() in wanted]
        missing = wanted - {r["name"].lower() for r in repos}
        if missing:
            print(f"Not cataloged: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    # Findings are a point-in-time snapshot. Stamp the run so anything derived from it (an onboarding
    # issue, a report) carries its own freshness signal and a reader can tell whether it still applies.
    run_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hub = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT)
    hub_sha = hub.stdout.strip() if hub.returncode == 0 else "unknown"
    print(f"audit run {run_utc} | hub {hub_sha}")
    if not HUB_NAME_FROM_REMOTE:
        print(f"warning: no git remote; template-reference check falls back to the directory name '{HUB_NAME}' and may miss", file=sys.stderr)
    print()

    hard = 0
    for entry in repos:
        model = entry.get("workflowModel") or spec["registry"].get("defaults", {}).get("workflowModel") or "release"
        ground = entry.get("groundTruthBranch", "main")
        try:
            findings, audited_sha = audit_repo(entry, spec)
        except Exception as e:  # a gh/JSON failure mid-audit must not abort the sweep
            findings, audited_sha = [("ERROR", str(e))], ""
        stamp = f" @ {ground}@{audited_sha[:7]}" if audited_sha else ""
        print(f"== {entry['name']} ({', '.join(entry.get('types', []))}; {model}){stamp} ==")
        if not findings:
            print("  clean (deterministic checks; the full letter+intent verdict is AUDIT.md's)")
        for kind, text in findings:
            print(f"  {kind:6} {text}")
            if kind in ("DEFECT", "LETTER", "ERROR"):
                hard += 1
    print(f"\n{len(repos)} repo(s) audited; {hard} defect/letter/error finding(s).")
    print("Findings are a point-in-time snapshot: re-run this audit before acting on them, and quote the")
    print("run stamp above in any issue derived from it (AUDIT.md section 8).")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Live fleet audit: the deterministic subset of AUDIT.md, run from the hub (stdlib + gh).

Compares each cataloged registry repo against the ground truth in this repo - general settings
(repo-config/settings.json), branch rulesets (normalized diff vs the model's payloads), secret
names (spec/secrets.json; values are never read), baseline/per-type file presence on the
ground-truth branch (spec/files.json), and branch-model facts (main/develop existence, develop
behind main). Owner-initiated: run it when onboarding a repo, when drift is suspected, or before
fleet-wide changes. Read-only - it never modifies a target.

Findings: DEFECT (an applicable check fails outright), LETTER (a required file is absent - intent
unverified, judge per AUDIT.md section 7), DRIFT (non-breaking divergence, e.g. main carrying
content develop lacks, a stale secret, a registry field contradicting reality), ERROR (a gh call
failed, so the repo could not be fully audited). Exits non-zero when any repo has a DEFECT,
LETTER, or ERROR finding.

Usage: python3 spec/audit.py [RepoName ...]   (default: every cataloged repo)
"""
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

SETTINGS_KEYS = [
    "has_wiki", "has_projects", "allow_merge_commit", "allow_squash_merge",
    "allow_rebase_merge", "allow_auto_merge", "allow_update_branch", "delete_branch_on_merge",
]
RULESET_SUBSET = ["name", "target", "enforcement", "bypass_actors", "conditions", "rules"]


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


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


def audit_repo(entry, spec):
    findings = []  # (kind, text)
    slug = repo_slug(entry)
    types = entry.get("types", [])
    model = entry.get("workflowModel") or spec["registry"].get("defaults", {}).get("workflowModel") or "release"
    ground = entry.get("groundTruthBranch", "main")

    try:
        live = gh(f"repos/{slug}")
    except RuntimeError as e:
        return [("ERROR", str(e))]

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
        # trees are identical. Content is the signal - a develop...main compare with changed files means
        # main carries content develop lacks (forward-sync needed); develop merely ahead is normal.
        if branch_main["commit"]["commit"]["tree"]["sha"] != branch_dev["commit"]["commit"]["tree"]["sha"]:
            cmp = gh(f"repos/{slug}/compare/develop...main", ok404=True)
            if cmp:
                # The three-dot compare's files[] is blind to cherry-picked promotions (develop may
                # already hold identical content under different commit SHAs, e.g. promote/* branches)
                # AND capped at 300 entries - so neither raw files[] nor a filter over it is reliable
                # (#336). Instead, derive the main-side change set from the merge-base tree (paths
                # whose blob differs base->main, additions and deletions included - no cap), then drop
                # paths whose blobs already match at develop: content develop already has is not
                # "content develop lacks". Three recursive trees calls; if any tree is truncated the
                # filter is skipped and the compare's unfiltered count kept (conservative, marked).
                trees = {
                    "base": gh(f"repos/{slug}/git/trees/{cmp['merge_base_commit']['commit']['tree']['sha']}?recursive=1"),
                    "develop": gh(f"repos/{slug}/git/trees/{branch_dev['commit']['commit']['tree']['sha']}?recursive=1"),
                    "main": gh(f"repos/{slug}/git/trees/{branch_main['commit']['commit']['tree']['sha']}?recursive=1"),
                }
                if any(t.get("truncated") for t in trees.values()):
                    if cmp.get("files"):
                        findings.append(("DRIFT", f"branch: main carries {len(cmp['files'])}+ changed file(s) develop lacks (forward-sync needed; tree too large to blob-filter cherry-pick noise)"))
                else:
                    blobs = {name: {e["path"]: e["sha"] for e in t["tree"] if e["type"] == "blob"} for name, t in trees.items()}
                    changed_on_main = {p for p in set(blobs["base"]) | set(blobs["main"]) if blobs["base"].get(p) != blobs["main"].get(p)}
                    lacking = sorted(p for p in changed_on_main if blobs["main"].get(p) != blobs["develop"].get(p))
                    if lacking:
                        shown = ", ".join(lacking[:8]) + (" ..." if len(lacking) > 8 else "")
                        findings.append(("DRIFT", f"branch: main carries {len(lacking)} file(s) develop lacks (forward-sync needed): {shown}"))

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

    # --- File presence on the ground-truth branch ---
    seen_paths = set()
    for item in spec["files"]["baseline"]:
        applies = item.get("appliesTo", "*")
        if applies != "*" and not set(applies) & set(types):
            continue
        path = item["path"]
        if path == "repo-config/develop.json" and model == "operational":
            path = "repo-config/operational/develop.json"
        if path in seen_paths:
            continue
        seen_paths.add(path)
        if gh(f"repos/{slug}/contents/{path}?ref={ground}", ok404=True) is None:
            findings.append(("LETTER", f"file: {path} absent on {ground} (verify intent per AUDIT.md section 7)"))

    return findings


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

    hard = 0
    for entry in repos:
        model = entry.get("workflowModel") or spec["registry"].get("defaults", {}).get("workflowModel") or "release"
        print(f"== {entry['name']} ({', '.join(entry.get('types', []))}; {model}) ==")
        try:
            findings = audit_repo(entry, spec)
        except Exception as e:  # a gh/JSON failure mid-audit must not abort the sweep
            findings = [("ERROR", str(e))]
        if not findings:
            print("  clean (deterministic checks; the full operational verdict is AUDIT.md's)")
        for kind, text in findings:
            print(f"  {kind:6} {text}")
            if kind in ("DEFECT", "LETTER", "ERROR"):
                hard += 1
    print(f"\n{len(repos)} repo(s) audited; {hard} defect/letter/error finding(s).")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())

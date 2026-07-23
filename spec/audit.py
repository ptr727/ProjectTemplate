#!/usr/bin/env python3
"""Live fleet audit: the deterministic subset of AUDIT.md, run from the hub (stdlib + gh).

Compares each cataloged registry repo against the ground truth in this repo - general settings
(repo-config/settings.json), branch rulesets (normalized diff vs the model's payloads), secret
names (spec/secrets.json; values are never read), baseline/per-type file presence and per-scope
markdown section presence on the ground-truth branch (spec/files.json, spec/scope-model.md), and
branch-model facts (main/develop existence, develop behind main). Owner-initiated: run it when
onboarding a repo, when drift is suspected, or before fleet-wide changes. Read-only - it never
modifies a target.

Findings: DEFECT (an applicable check fails outright), LETTER (a required file is absent - intent
unverified, judge per AUDIT.md section 7), DRIFT (non-breaking divergence, e.g. main-side
changes develop lacks, a stale secret, a registry field contradicting reality), ERROR (a gh call
failed, so the repo could not be fully audited). Exits non-zero when any repo has a DEFECT,
LETTER, or ERROR finding.

Usage: python3 spec/audit.py [RepoName ...]   (default: every cataloged repo)
"""
import base64
import functools
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

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


def gh(path, ok404=False) -> Any:
    """GET a REST path via gh, returning parsed JSON, or None on a 404 (when ok404) or an empty response body.

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
    a flat token set is unambiguous. workflowModel and releaseTrigger resolve repo -> defaults -> fleet
    default (as configure.sh does). consumerModel has no fleet default - validate.py requires it on every
    cataloged repo, so a cataloged repo always contributes one.
    """
    sel = set(entry.get("types", []))
    sel.add(entry.get("workflowModel") or defaults.get("workflowModel") or "release")
    sel.add(entry.get("releaseTrigger") or defaults.get("releaseTrigger") or "two-phase")
    # consumerModel has no defaults fallback - the registry schema does not allow defaults.consumerModel, and
    # validate.py requires it on every cataloged repo. The guard only shields a malformed non-cataloged entry.
    cm = entry.get("consumerModel")
    if cm:
        sel.add(cm)
    return sel


def applies(applies_to, sel):
    """True if an appliesTo selector applies to a repo's selector set. Disjunctive any-of, with `*` meaning all."""
    if applies_to == "*":
        return True
    tokens = applies_to if isinstance(applies_to, list) else [applies_to]
    return bool(set(tokens) & sel)


_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")


def _section_spec(elt):
    """Normalize a sections[] entry to (name, appliesTo, fidelity). A bare string is appliesTo `*`, intent."""
    if isinstance(elt, str):
        return elt, "*", "intent"
    return elt.get("name", ""), elt.get("appliesTo", "*"), elt.get("fidelity", "intent")


def required_sections(item, sel):
    """Section names to presence-check (heading grep) for this repo - the non-verbatim sections.

    A bare-string section is appliesTo `*`, intent; an object section carries its own selector and fidelity.
    A verbatim section is checked byte-for-byte instead (verbatim_sections), so it is excluded here to avoid a
    redundant presence finding. The entry's own appliesTo is assumed already matched by the caller.
    """
    out = []
    for elt in item.get("sections", []):
        name, sec, fid = _section_spec(elt)
        if name and fid != "verbatim" and applies(sec, sel):
            out.append(name)
    return out


def verbatim_sections(item, sel):
    """Section names marked fidelity verbatim for this repo - checked byte-for-byte against the hub canonical."""
    out = []
    for elt in item.get("sections", []):
        name, sec, fid = _section_spec(elt)
        if name and fid == "verbatim" and applies(sec, sel):
            out.append(name)
    return out


def extract_section(text, heading):
    """The `## <heading>` H2 section including its heading line, up to the next sibling H2 or EOF; None if absent.

    EOL-normalized to `\\n`. The match that locates the heading is by its parsed text (the text after the `## `
    marker, case- and whitespace-folded), so a re-cased or re-spaced heading is still found rather than read as
    a missing section. The heading line's exact bytes are then part of the hashed region, so that re-casing or
    re-spacing surfaces as drift. A nested `###` stays inside the body. A `## ` line inside a fenced code block
    (``` or ~~~) is not a boundary, so a code sample cannot truncate the region and hide drift after it.
    """
    want = heading.strip().lower()
    out, capturing, fenced = [], False, False
    for ln in normalize(text).split("\n"):
        stripped = ln.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
        elif not fenced and stripped.startswith("## "):
            if capturing:
                break  # a sibling H2 ends the section
            if stripped[2:].strip().lower() == want:  # parsed heading text after the "## " marker
                capturing = True
                out.append(ln)  # include the heading so its exact bytes are part of the hash
            continue
        if capturing:
            out.append(ln)
    return "\n".join(out) if capturing else None


def heading_texts(markdown):
    """Lowercased heading texts in a markdown document, for case-insensitive section-presence matching."""
    return {m.group(1).strip().lower() for line in markdown.splitlines() for m in (_HEADING.match(line),) if m}


_JOB_KEY = re.compile(r"^([A-Za-z0-9_.\-]+):(\s.*)?$")


def split_jobs(text):
    """Slice a workflow YAML into {job_key: block_text} by the jobs-level indent, without a YAML parser.

    Structural, not semantic: find the top-level `jobs:` key, take the indent of its first child as the
    job-key indent, and cut the region at each key line at exactly that indent. A line dedented below the
    job-key indent (a sibling top-level key) ends the jobs region.
    """
    lines = text.splitlines(keepends=True)
    ji = next((i for i, ln in enumerate(lines) if re.match(r"^jobs:\s*(#.*)?$", ln)), None)
    if ji is None:
        return {}
    job_indent = None
    for ln in lines[ji + 1:]:
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            continue
        job_indent = len(ln) - len(ln.lstrip())
        break
    if not job_indent:
        return {}
    blocks, key, cur = {}, None, []
    for ln in lines[ji + 1:]:
        indent = len(ln) - len(ln.lstrip())
        if ln.strip() and indent < job_indent:
            if ln.lstrip().startswith("#"):
                continue  # a dedented comment is not part of any job and does not end the mapping - skip it
            break  # a sibling top-level key ends the jobs region
        m = _JOB_KEY.match(ln[job_indent:]) if indent == job_indent else None
        if m:
            if key is not None:
                blocks[key] = "".join(cur)
            key, cur = m.group(1), [ln]  # include the key line so an inline mapping on it is captured
        elif key is not None:
            cur.append(ln)
    if key is not None:
        blocks[key] = "".join(cur)
    return blocks


def _code_view(text):
    """Workflow text with comment-only lines dropped, so a token mentioned only in a comment is not signal.

    A carried task file documents its own contract in comments (build-release-task.yml names
    `release-asset-` and `artifact-ids:` in prose), so a raw substring search over the whole text would
    both false-pass a missing handoff and false-flag a forbidden token that appears only in a comment.
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def job_level_names(blocks):
    """The job-level `name:` values - a job's own direct-child name key, not a step's name.

    The ruleset-bound check is a job name, so a step happening to share the string must not satisfy it. A
    job's direct children sit at the shallowest indent of its block (below the key line); a step name is
    deeper or a `- name:` list item, so neither reaches that indent as a bare `name:`.
    """
    out = set()
    for block in blocks.values():
        body = [ln for ln in block.splitlines()[1:] if ln.strip() and not ln.lstrip().startswith("#")]
        if not body:
            continue
        child = min(len(ln) - len(ln.lstrip()) for ln in body)
        for ln in body:
            if len(ln) - len(ln.lstrip()) == child:
                m = re.match(r"name:\s*['\"]?(.*?)['\"]?\s*$", ln[child:])
                if m:
                    out.add(m.group(1))
    return out


def check_interface(path, contract, text):
    """Verify a workflow honors its fixed contract by name and wiring, never its body (spec/fidelity-model.md).

    All findings are DRIFT: the interface is what a repo must keep, the body is owned, and a rename or a
    forked handoff is a hint to verify, not a hard failure. `verbatimJobs` is the verbatim engine's concern.
    """
    findings = []
    jobs = split_jobs(text)
    code = _code_view(text)
    for k in contract.get("requiredJobKeys", []):
        if k not in jobs:
            findings.append(("DRIFT", f"interface: {path} missing required job '{k}'"))
    name = contract.get("requiredCheckName")
    if name and name not in job_level_names(jobs):
        findings.append(("DRIFT", f"interface: {path} missing the ruleset-bound check name '{name}' as a job name"))
    tok = contract.get("artifactNameToken")
    if tok and tok not in code:
        findings.append(("DRIFT", f"interface: {path} missing the '{tok}<branch>-<target>' artifact handoff"))
    # Token checks only apply to a job that is present; an absent job is already reported by requiredJobKeys,
    # so skip it rather than emit a redundant "missing token" for every token it cannot contain. Scan the
    # job's code view so a token in a comment is not read as signal.
    for job, toks in contract.get("requireTokensInJob", {}).items():
        if job in jobs:
            block = _code_view(jobs[job])
            for t in toks:
                if t not in block:
                    findings.append(("DRIFT", f"interface: {path} job '{job}' missing required '{t}'"))
    for job, toks in contract.get("forbidTokensInJob", {}).items():
        if job in jobs:
            block = _code_view(jobs[job])
            for t in toks:
                if t in block:
                    findings.append(("DRIFT", f"interface: {path} job '{job}' uses forbidden '{t}' (forks the verbatim github-release download - see AGENTS.md override seam)"))
    return findings


def normalize(text):
    """Reduce a carried unit to its comparable form: neutralize line endings, since EOL variance is governed
    separately, not a fidelity deviation. No placeholder masking - see spec/fidelity-model.md "Normalization".
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


@functools.lru_cache(maxsize=1024)  # bounded; the keys that recur across repos are the canonical and its history
def _hash_normalized(norm_text):
    return hashlib.sha256(norm_text.encode("utf-8")).hexdigest()


def content_hash(text):
    # Cache on the normalized form, not raw text, so EOL-only variants (CRLF vs LF) share one entry.
    return _hash_normalized(normalize(text))


def classify_verbatim(down_text, canon_text, past_texts):
    """None if the downstream copy matches the current canonical, 'stale' if it matches a past hub revision
    (the base advanced - re-vendor), or 'modified' if it matches no revision the base ever produced (the
    repo changed fixed content). The discriminator is a content hash, never a version stamp - a stamp can
    claim to be current while the body was edited, so it is never trusted for integrity.
    """
    dh = content_hash(down_text)
    if dh == content_hash(canon_text):
        return None
    for past in past_texts:
        if content_hash(past) == dh:
            return "stale"
    return "modified"


_HISTORY_CACHE: dict[str, list[str]] = {}  # rel_path -> past revision contents, cached because one canonical is compared against every audited repo


def git_file_history(rel_path):
    """Every past revision's content of a hub-tracked file (to tell a stale copy from a modified one), cached per rel_path."""
    if rel_path in _HISTORY_CACHE:
        return _HISTORY_CACHE[rel_path]
    out = []
    # Decode as UTF-8/replace to match the downstream and canonical reads. A divergent decode would fabricate a mismatch.
    r = subprocess.run(["git", "log", "--format=%H", "--", rel_path], cwd=ROOT, capture_output=True,
                       encoding="utf-8", errors="replace")
    if r.returncode == 0:
        for sha in r.stdout.split():
            s = subprocess.run(["git", "show", f"{sha}:{rel_path}"], cwd=ROOT, capture_output=True,
                               encoding="utf-8", errors="replace")
            if s.returncode == 0:
                out.append(s.stdout)
    _HISTORY_CACHE[rel_path] = out
    return out


def check_verbatim(label, down_text, canonical_rel, extract=None):
    """Compare a downstream copy against the hub's canonical (a region if `extract` is given), EOL-normalized,
    and classify a mismatch as stale or modified via the canonical's git history. All findings are DRIFT: a
    byte diff is a hint to review, never proof of breakage.
    """
    try:
        # Same decode policy as the downstream copy and the git history, so a stray byte can never make
        # otherwise-equal content hash differently across the three sources.
        canon_text = (ROOT / canonical_rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [("DRIFT", f"verbatim: {label} canonical {canonical_rel} is unreadable from the hub (spec error?)")]
    history = git_file_history(canonical_rel)
    if extract is not None:
        down_region, canon_region = extract(down_text), extract(canon_text)
        if canon_region is None:
            return [("DRIFT", f"verbatim: {label} region absent in the canonical (spec error?)")]
        if down_region is None:
            return [("DRIFT", f"verbatim: {label} region absent downstream, cannot compare")]
        down_text, canon_text = down_region, canon_region
        history = [h for h in (extract(t) for t in history) if h is not None]
    verdict = classify_verbatim(down_text, canon_text, history)
    if verdict is None:
        return []
    if verdict == "stale":
        return [("DRIFT", f"verbatim: {label} matches a past hub revision, not the current canonical - the base advanced, re-vendor it")]
    return [("DRIFT", f"verbatim: {label} differs from the canonical and matches no past hub revision - the repo modified fixed content, review it")]


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
                # develop already has is not "content develop lacks". Three recursive tree calls, so if
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
    # implicit baseline). Mechanism-mapped names already carry their stores above, and unmapped ones are
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
    # only. Whether each declared ecosystem dual-targets main+develop (the fleet norm) is verified by
    # inspection, not here. Only runs when dependabot.yml exists, since its absence is already a file-presence
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

    # --- File and section presence on the ground-truth branch ---
    # appliesTo is matched against the repo's full selector set (types + workflowModel + releaseTrigger +
    # consumerModel), so the release/operational develop ruleset is two data entries, not a code swap.
    # Required sections union across same-path entries. A carried markdown file must contain each heading
    # scoped to this repo. A rename reads as missing and equivalence is judged by hand, so a missing section
    # is DRIFT (a hint to verify), never a LETTER.
    sel = repo_selectors(entry, spec["registry"].get("defaults", {}))
    wanted_sections = {}  # path -> set of required section names, unioned across applicable entries
    verbatim_secs = {}  # path -> set of section names checked byte-for-byte against the hub canonical
    check_item = {}  # path -> entry, for a fidelity interface/verbatim entry (last applicable wins per path)
    path_order = []
    for item in spec["files"]["baseline"]:
        if not applies(item.get("appliesTo", "*"), sel):
            continue
        path = item["path"]
        if path not in wanted_sections:
            wanted_sections[path] = set()
            verbatim_secs[path] = set()
            path_order.append(path)
        wanted_sections[path].update(required_sections(item, sel))
        verbatim_secs[path].update(verbatim_sections(item, sel))
        if item.get("fidelity") in ("interface", "verbatim"):
            check_item[path] = item
    for path in path_order:
        content = gh(f"repos/{slug}/contents/{path}?ref={ground}", ok404=True)
        item = check_item.get(path)
        fid = item.get("fidelity") if item else "presence"
        if content is None:
            # An interface unit's presence is DRIFT, not LETTER - a workflow's naming is more variable than a
            # carried config, so absence is a hint to verify. Any other unit's absence is a file-presence LETTER.
            if fid == "interface":
                findings.append(("DRIFT", f"interface: {path} absent on {ground}, cannot verify its contract"))
            else:
                findings.append(("LETTER", f"file: {path} absent on {ground} (verify intent per AUDIT.md section 7)"))
            continue
        # Guard on encoding, not truthiness: an empty file returns encoding "base64" with content "" (decode it
        # to ""), whereas a too-large or non-inline payload returns encoding "none" (text stays None -> flagged).
        text = base64.b64decode(content["content"]).decode("utf-8", "replace") if content.get("encoding") == "base64" else None
        # Interface conformance (name + wiring) plus any verbatim job regions the contract pins.
        if item is not None and fid == "interface":
            if text is None:
                findings.append(("DRIFT", f"interface: could not read {path} content on {ground} to verify its contract (no inline content returned); verify by hand"))
            else:
                contract = item.get("contract", {})
                findings.extend(check_interface(path, contract, text))
                canonical_rel = item.get("reference") or path
                for job in contract.get("verbatimJobs", []):
                    findings.extend(check_verbatim(f"{path} job '{job}'", text, canonical_rel,
                                                   extract=lambda t, j=job: split_jobs(t).get(j)))
        # Whole-file verbatim: byte-identical to the hub's canonical after EOL normalization.
        elif item is not None and fid == "verbatim":
            if text is None:
                findings.append(("DRIFT", f"verbatim: could not read {path} content on {ground} to compare (no inline content returned); verify by hand"))
            else:
                findings.extend(check_verbatim(path, text, item.get("reference") or path))
        # Heading-based presence is only meaningful for markdown. A "section" named on a non-md file (e.g. a
        # tasks.json task group) is an intent marker judged per AUDIT.md, not a heading grep.
        needed = wanted_sections[path]
        verbatim_needed = verbatim_secs[path]
        if (needed or verbatim_needed) and path.endswith(".md"):
            if text is None:
                # Fail loud rather than skip silently: the contents API returned no inline content (an
                # oversized file, a symlink, a submodule), so the section check could not run - surface that
                # instead of a false clean.
                findings.append(("DRIFT", f"section: could not read {path} content on {ground} to verify sections (no inline content returned); verify by hand"))
            else:
                present = heading_texts(text)
                for name in sorted(needed):
                    if name.strip().lower() not in present:
                        findings.append(("DRIFT", f"section: '{name}' not found as a heading in {path} on {ground} (renamed or missing; verify intent per AUDIT.md section 7)"))
                # A verbatim section must match the hub's canonical byte-for-byte (EOL-normalized), like a
                # verbatim file but scoped to the one `## <heading>` region - so a universal rule block cannot
                # drift or fall behind a newly added rule while its heading still passes the presence check.
                for name in sorted(verbatim_needed):
                    findings.extend(check_verbatim(f"{path} section '{name}'", text, path,
                                                   extract=lambda t, n=name: extract_section(t, n)))

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


def _selftest():
    """Exercise the interface engine on synthetic fixtures, no network - verify the mechanism, not the fleet."""
    pr_check = "  check-workflow-status:\n    name: Check pull request workflow status job\n    needs: [changes]\n    runs-on: ubuntu-latest\n"
    pr_head = "name: Test\non: pull_request\njobs:\n  changes:\n    runs-on: ubuntu-latest\n"
    pr_contract = {"requiredJobKeys": ["check-workflow-status"], "requiredCheckName": "Check pull request workflow status job"}
    gh_rel = ("  github-release:\n    needs: [get-version, build-widget]\n    runs-on: ubuntu-latest\n    steps:\n"
              "      - uses: actions/download-artifact@v4\n        with:\n          pattern: release-asset-${{ inputs.branch }}-*\n          merge-multiple: true\n")
    rel_head = ("name: Build Release\non:\n  workflow_call:\njobs:\n"
                "  get-version:\n    runs-on: ubuntu-latest\n    steps: []\n"
                "  build-widget:\n    runs-on: ubuntu-latest\n    steps: []\n")
    rel_ok = rel_head + gh_rel
    rel_contract = {"requiredJobKeys": ["get-version", "github-release"], "artifactNameToken": "release-asset-",
                    "requireTokensInJob": {"github-release": ["pattern:", "merge-multiple:"]},
                    "forbidTokensInJob": {"github-release": ["artifact-ids:"]}}
    cases = [
        ("conformant PR workflow", pr_head + pr_check, pr_contract, 0),
        ("PR workflow missing the required job and its check name", pr_head, pr_contract, 2),
        ("PR workflow with a renamed check", pr_head + pr_check.replace("Check pull request workflow status job", "Renamed"), pr_contract, 1),
        ("check name present only in a run step, not as a job name", pr_head + "  check-workflow-status:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo Check pull request workflow status job\n", pr_contract, 1),
        ("check name present only as a step name, not the job name", pr_head + "  check-workflow-status:\n    runs-on: ubuntu-latest\n    steps:\n      - name: Check pull request workflow status job\n        run: true\n", pr_contract, 1),
        ("conformant release task", rel_ok, rel_contract, 0),
        ("release task with an artifact-ids fork in github-release", rel_ok.replace("          merge-multiple: true\n", "          merge-multiple: true\n          artifact-ids: 123\n"), rel_contract, 1),
        ("release task missing merge-multiple in github-release", rel_ok.replace("          merge-multiple: true\n", ""), rel_contract, 1),
        ("release task with an owned extra leaf job", rel_head + "  build-extra:\n    runs-on: ubuntu-latest\n    steps: []\n" + gh_rel, rel_contract, 0),
        ("absent job reports once, no redundant token findings", rel_head, {"requiredJobKeys": ["github-release"], "requireTokensInJob": {"github-release": ["pattern:", "merge-multiple:"]}}, 1),
        ("a forbidden token only in a comment is ignored", rel_ok.replace("          merge-multiple: true\n", "          merge-multiple: true\n          # never an artifact-ids: fork here\n"), rel_contract, 0),
        ("a required token only in a comment does not count", rel_ok.replace("          merge-multiple: true\n", "          # merge-multiple: true (was here)\n"), rel_contract, 1),
    ]
    ok = True
    for label, text, contract, want in cases:
        got = len(check_interface("wf", contract, text))
        if got != want:
            ok = False
        print(f"  {'ok  ' if got == want else 'FAIL'} want={want} got={got}  {label}")
    trailing = split_jobs(rel_ok + "# a trailing top-level comment\n")
    if set(trailing) != {"get-version", "build-widget", "github-release"} or "trailing top-level comment" in trailing.get("github-release", ""):
        ok = False
        print(f"  FAIL split_jobs (trailing comment) -> {sorted(trailing)}")
    else:
        print(f"  ok   split_jobs (trailing comment) -> {sorted(trailing)}")
    inline = split_jobs("name: X\non: push\njobs:\n  quick: {runs-on: ubuntu-latest}\n  full:\n    runs-on: ubuntu-latest\n")
    if set(inline) != {"quick", "full"} or "runs-on" not in inline.get("quick", ""):
        ok = False
        print(f"  FAIL split_jobs (inline mapping) -> {sorted(inline)}")
    else:
        print("  ok   split_jobs (inline-mapping job captured with its content)")

    # Verbatim engine: EOL normalization, hashing, and the stale-vs-modified classification. Exercised here
    # rather than only in production, because a latent bug in the comparison would otherwise surface as a
    # false clean on a real fleet run.
    canon = "line one\nline two\nline three\n"
    verbatim_cases = [
        # (label, down_text, canon_text, history, want)
        ("identical -> match", canon, canon, [], None),
        ("EOL-only diff (CRLF) -> match", canon.replace("\n", "\r\n"), canon, [], None),
        ("EOL-only diff (bare CR) -> match", canon.replace("\n", "\r"), canon, [], None),
        ("body edit -> modified", canon.replace("line two", "line TWO edited"), canon, [], "modified"),
        ("matches a past revision -> stale", "old body\n", "current body\n", ["old body\n", "older\n"], "stale"),
        ("matches a past revision modulo EOL -> stale", "old body\r\n", "current body\n", ["old body\n"], "stale"),
        ("edit in no revision -> modified", "never existed\n", "current body\n", ["old body\n"], "modified"),
    ]
    for label, down, canon_t, history, want in verbatim_cases:
        got = classify_verbatim(down, canon_t, history)
        if got != want:
            ok = False
        print(f"  {'ok  ' if got == want else 'FAIL'} want={str(want):>8} got={str(got):>8}  verbatim: {label}")
    # Region extraction and hashing: a forked github-release block must hash differently from the canonical.
    region = split_jobs(rel_ok).get("github-release")
    forked_region = split_jobs(rel_ok.replace("          merge-multiple: true\n", "          artifact-ids: 1\n")).get("github-release")
    if region is None or forked_region is None or content_hash(region) == content_hash(forked_region):
        ok = False
        print("  FAIL verbatim: forked github-release region should hash differently")
    else:
        print("  ok   verbatim: a forked github-release region hashes differently from the canonical")
    # Section-region extraction: the region includes the heading line, keeps a nested ### and a fenced ## inside
    # the body, ends at the next sibling H2, is None if absent, and rehashes when the heading is re-cased - the
    # per-section verbatim check depends on every one of these.
    md = "# Title\n\n## Alpha\n\nbody a\n\n```\n## not a heading\n```\n\n### nested\nstill alpha\n\n## Beta\n\nbody b\n"
    a, b, gone = extract_section(md, "Alpha"), extract_section(md, "Beta"), extract_section(md, "Gamma")
    spaced = extract_section("##   Alpha\n\nbody a\n", "Alpha")  # extra marker-gap whitespace still locates
    if (a is None or not a.startswith("## Alpha") or "body a" not in a or "## not a heading" not in a
            or "still alpha" not in a or "body b" in a
            or b is None or not b.startswith("## Beta") or "body b" not in b or "body a" in b or gone is not None
            or spaced is None or not spaced.startswith("##   Alpha")
            or content_hash(a) == content_hash(extract_section(md.replace("## Alpha", "## alpha"), "Alpha"))):
        ok = False
        print("  FAIL section: extract_section region/hash behavior")
    else:
        print("  ok   section: heading in region, fenced ## kept, sibling H2 ends, None if absent, whitespace-tolerant locate, re-cased heading rehashes")

    # Issue generator: findings land in the right buckets and the title carries the count.
    fe = {"name": "Widget", "types": ["python"]}
    it, ib = render_issue(fe, [("LETTER", "file: X absent"), ("DRIFT", "verbatim: Y differs"), ("ERROR", "gh failed")],
                          "main", "abc1234", "2026-01-01T00:00:00Z", "hub123")
    et, eb = render_issue(fe, [], "main", "abc1234", "2026-01-01T00:00:00Z", "hub123")
    if ("Widget" not in it or "(3 findings)" not in it or "## Must fix" not in ib
            or "**LETTER** file: X absent" not in ib or "## Converge" not in ib or "verbatim: Y differs" not in ib
            or "## Could not verify" not in ib or "(0 findings)" not in et or "nothing to converge" not in eb):
        ok = False
        print("  FAIL issue: render_issue grouping/title/empty behavior")
    else:
        print("  ok   issue: render_issue groups must-fix/converge/unverifiable, counts findings, handles the clean case")

    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return 0 if ok else 1


def render_issue(entry, findings, ground, audited_sha, run_utc, hub_sha):
    """A ready-to-file convergence issue (title, body) generated from one repo's audit findings.

    The content is a view over the audit, not composed by hand, so it is always accurate and regenerable -
    re-run the audit, re-generate the issue. Findings are grouped by what the maintainer does with them:
    presence/contract findings to fix, drift to converge (re-vendor or review), and anything unverifiable.
    """
    name = entry["name"]
    types = ", ".join(entry.get("types", [])) or "untyped"
    stamp = f"{ground}@{audited_sha[:7]}" if audited_sha else ground
    blocking = [(k, t) for k, t in findings if k in ("DEFECT", "LETTER")]
    drift = [t for k, t in findings if k == "DRIFT"]
    errors = [t for k, t in findings if k == "ERROR"]
    title = f"Converge {name} with the hub baseline ({len(findings)} finding{'' if len(findings) == 1 else 's'})"

    out = []
    w = out.append
    # No H1 - GitHub renders the issue title as the heading, so an H1 here would duplicate it.
    w(f"Generated from the hub audit of `{name}` ({types}) at `{stamp}`, hub `{hub_sha}`, {run_utc}. "
      f"Regenerate with `spec/audit.py --issue {name}`. Findings are a point-in-time snapshot - re-run the "
      f"audit before acting (AUDIT.md section 8). This lists what the audit mechanically detects. The full "
      f"letter and intent verdict lives in AUDIT.md.")
    w("")
    if not findings:
        w("The deterministic checks are clean - nothing to converge.")
        return title, "\n".join(out) + "\n"
    if blocking:
        w("## Must fix")
        w("")
        w("A missing carried file, or a broken workflow contract.")
        w("")
        for k, t in blocking:
            w(f"- **{k}** {t}")
        w("")
    if drift:
        w("## Converge")
        w("")
        w("Divergences from the hub canonical. A `verbatim:` file or section re-vendors the current hub copy "
          "byte-for-byte (a `section` re-vendors just that one `## heading` block). An `interface:` item must "
          "honor the named workflow contract. A stale-but-present copy is re-vendored; a genuinely repo-specific "
          "difference is judged by meaning per AUDIT.md.")
        w("")
        for t in drift:
            w(f"- {t}")
        w("")
    if errors:
        w("## Could not verify")
        w("")
        w("The audit could not read something it needed. Re-run once the cause is cleared.")
        w("")
        for t in errors:
            w(f"- {t}")
        w("")
    return title, "\n".join(out).rstrip() + "\n"


def main():
    if "--selftest" in sys.argv:
        return _selftest()
    spec = {
        "registry": load("registry/repos.json"),
        "settings": load("repo-config/settings.json"),
        "secrets": load("spec/secrets.json"),
        "files": load("spec/files.json"),
    }
    issue_mode = "--issue" in sys.argv
    wanted = {n.lower() for n in sys.argv[1:] if not n.startswith("--")}
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

    # --issue <repo>: emit a ready-to-file convergence issue for one repo (title on the first line, then the
    # body) - stdout carries only the issue so it can be captured and filed. The body is generated from the
    # audit, so it never drifts from reality.
    if issue_mode:
        if len(repos) != 1:
            print("--issue takes exactly one cataloged repo", file=sys.stderr)
            return 2
        entry = repos[0]
        try:
            findings, audited_sha = audit_repo(entry, spec)
        except Exception as e:  # an unverifiable audit still produces an honest issue
            findings, audited_sha = [("ERROR", str(e))], ""
        title, body = render_issue(entry, findings, entry.get("groundTruthBranch", "main"),
                                   audited_sha, run_utc, hub_sha)
        print(f"TITLE: {title}")
        print()
        print(body, end="")
        return 0

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

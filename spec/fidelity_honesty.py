#!/usr/bin/env python3
"""Fidelity-honesty analysis: check the manifest's declared fidelities against fleet reality.

Read-only, owner-run, not wired into CI. Reuses spec/audit.py's fleet machinery (gh, content_hash,
git history, selectors) - import-safe because audit.py guards its main.

The verbatim engine verifies a unit's *content* against the canonical (declared -> hashed). This tool
verifies the *classifications themselves* (declared -> checked), the same declared-to-verified leap one
level up. It answers two questions the audit cannot:

  1. Which `intent` units are actually content-identical (after EOL normalization) across the whole fleet? Those are candidates to
     promote to `verbatim` - they would gain free drift-detection (a stale-but-present copy is invisible
     under intent, caught under verbatim). This is the class that hid the configure.sh drift.
  2. Which `verbatim` units have a downstream copy that diverges in a NON-stale way? That is either a
     mis-set label (the content legitimately varies -> should be intent) or real drift to chase.

It also runs a manifest-gap pass: a file present in BOTH the hub and a reference adopter but absent from
the manifest is carried-but-untracked (exactly the configure.sh / settings.json bug).

Usage: python3 spec/fidelity_honesty.py [reference-repo-for-manifest-gap]   (default: Financial-Modeling)
"""
import base64
import sys

import audit  # sibling; import-safe (its main is guarded)

REF_ADOPTER = "Financial-Modeling"  # a well-adopted repo, used only for the manifest-gap pass


def canonical_text(entry):
    """The hub's canonical for a unit: its reference snippet, else its own root copy."""
    ref = entry.get("reference") or entry["path"]
    try:
        return (audit.ROOT / ref).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def fetch(slug, path, ref):
    """Decoded downstream file content, or None if absent / not inline."""
    content = audit.gh(f"repos/{slug}/contents/{path}?ref={ref}", ok404=True)
    if content is None or content.get("encoding") != "base64":
        return None
    return base64.b64decode(content["content"]).decode("utf-8", "replace")


def fidelity_pass(spec):
    defaults = spec["registry"].get("defaults", {})
    repos = [r for r in spec["registry"]["repos"] if r.get("status") == "cataloged"]
    units = [e for e in spec["files"]["baseline"] if e.get("fidelity") in ("intent", "verbatim")]

    spreads = []          # (entry, spread dict) for the full table
    promote = []          # intent units that are uniform fleet-wide
    mislabel = []         # verbatim units that diverge non-stale

    for e in units:
        path, fid = e["path"], e["fidelity"]
        canon = canonical_text(e)
        if canon is None:
            spreads.append((e, None))
            continue
        canon_hash = audit.content_hash(canon)
        history = {audit.content_hash(t) for t in audit.git_file_history(e.get("reference") or path)}
        spread = {"match": [], "stale": [], "differs": [], "absent": []}
        for r in repos:
            if not audit.applies(e.get("appliesTo", "*"), audit.repo_selectors(r, defaults)):
                continue
            text = fetch(audit.repo_slug(r), path, r.get("groundTruthBranch", "main"))
            if text is None:
                spread["absent"].append(r["name"])
                continue
            dh = audit.content_hash(text)
            if dh == canon_hash:
                spread["match"].append(r["name"])
            elif dh in history:
                spread["stale"].append(r["name"])
            else:
                spread["differs"].append(r["name"])
        spreads.append((e, spread))
        present = spread["match"] + spread["stale"] + spread["differs"]
        if fid == "intent" and present and not spread["differs"]:
            promote.append((e, spread))
        if fid == "verbatim" and spread["differs"]:
            mislabel.append((e, spread))
    return spreads, promote, mislabel


def manifest_gap_pass(spec, ref_repo):
    """Files present in BOTH the hub and the reference adopter but absent from the manifest."""
    listed = {e["path"] for e in spec["files"]["baseline"]}
    entry = next((r for r in spec["registry"]["repos"] if r["name"] == ref_repo), None)
    if entry is None:
        return None, []
    slug = audit.repo_slug(entry)
    ground = entry.get("groundTruthBranch", "main")
    # The git/trees endpoint takes a tree SHA, not a ref name, so resolve the branch to its tree SHA
    # first (as audit.py does) - passing the branch name can 404 and silently drop the whole check.
    br = audit.gh(f"repos/{slug}/branches/{ground}", ok404=True)
    if not br or "commit" not in br:
        return slug, []
    tree = audit.gh(f"repos/{slug}/git/trees/{br['commit']['commit']['tree']['sha']}?recursive=1", ok404=True)
    if not tree or "tree" not in tree:
        return slug, []
    # Exclude only the .git metadata directory (by path component, not a substring - a substring check
    # is separator-dependent and also swallows .github/, .gitattributes, .gitignore).
    hub_files = {str(p.relative_to(audit.ROOT)).replace("\\", "/")
                 for p in audit.ROOT.rglob("*") if p.is_file() and ".git" not in p.parts}
    gaps = sorted(n["path"] for n in tree["tree"]
                  if n.get("type") == "blob" and n["path"] in hub_files and n["path"] not in listed)
    return slug, gaps


def main():
    ref_repo = sys.argv[1] if len(sys.argv) > 1 else REF_ADOPTER
    spec = {
        "registry": audit.load("registry/repos.json"),
        "files": audit.load("spec/files.json"),
    }
    spreads, promote, mislabel = fidelity_pass(spec)

    print("== Per-unit fleet spread (ground-truth branch per repo) ==")
    print("   fidelity path :: match / stale / differs / absent")
    for e, spread in spreads:
        if spread is None:
            print(f"   {e.get('fidelity'):8} {e['path']} :: canonical unreadable at hub - skipped")
            continue
        print(f"   {e['fidelity']:8} {e['path']} :: "
              f"{len(spread['match'])} / {len(spread['stale'])} / {len(spread['differs'])} / {len(spread['absent'])}")

    print("\n== INTENT units content-identical fleet-wide (after EOL normalization) -> candidates to promote to VERBATIM ==")
    print("   (uniform today, but intent cannot catch a future stale-but-present copy; verbatim can)")
    if not promote:
        print("   none")
    for e, spread in promote:
        print(f"   {e['path']}: {len(spread['match'])} match, {len(spread['stale'])} stale, "
              f"{len(spread['absent'])} absent, 0 differ")

    print("\n== VERBATIM units with NON-stale downstream divergence (mis-label or real drift) ==")
    if not mislabel:
        print("   none")
    for e, spread in mislabel:
        print(f"   {e['path']}: differs in {', '.join(spread['differs'])}")

    print(f"\n== Manifest gap: files carried by {ref_repo} + present at the hub but NOT in the manifest ==")
    slug, gaps = manifest_gap_pass(spec, ref_repo)
    if slug is None:
        print(f"   {ref_repo} not found in the registry")
    elif not gaps:
        print("   none - the manifest covers every hub file the reference adopter also carries")
    else:
        for g in gaps:
            print(f"   UNTRACKED  {g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

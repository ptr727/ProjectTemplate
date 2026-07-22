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
import subprocess
import sys

import audit  # sibling, import-safe (its main is guarded)

REF_ADOPTER = "Financial-Modeling"  # a well-adopted repo, used only for the manifest-gap pass
REPORT_PATH = "reports/divergences.md"  # the generated, checked-in burn-down report (--report)


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
        spread = {"match": [], "stale": [], "differs": [], "unavailable": []}
        for r in repos:
            if not audit.applies(e.get("appliesTo", "*"), audit.repo_selectors(r, defaults)):
                continue
            text = fetch(audit.repo_slug(r), path, r.get("groundTruthBranch", "main"))
            if text is None:  # missing (404) or present-but-non-inline (too large / encoding "none")
                spread["unavailable"].append(r["name"])
                continue
            dh = audit.content_hash(text)
            if dh == canon_hash:
                spread["match"].append(r["name"])
            elif dh in history:
                spread["stale"].append(r["name"])
            else:
                spread["differs"].append(r["name"])
        spreads.append((e, spread))
        # A verbatim candidate has NO hand-modified copy ("differs") and at least one confirmed match with
        # the current canonical. Stale copies do not disqualify it - verbatim would flag them "stale ->
        # re-vendor", which is the point. A unit that is entirely stale/unavailable is not confirmed uniform.
        if fid == "intent" and spread["match"] and not spread["differs"]:
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
    # Fail loud on an unreadable reference adopter: an empty gaps list would report "none" (a false clean).
    br = audit.gh(f"repos/{slug}/branches/{ground}", ok404=True)
    if not br or "commit" not in br:
        raise RuntimeError(f"could not read {slug}@{ground} (missing branch?) - cannot run the manifest-gap pass")
    tree = audit.gh(f"repos/{slug}/git/trees/{br['commit']['commit']['tree']['sha']}?recursive=1", ok404=True)
    if not tree or "tree" not in tree:
        raise RuntimeError(f"could not read the tree for {slug}@{ground} - cannot run the manifest-gap pass")
    # The hub's tracked files (git ls-files), not a filesystem walk: a walk pulls in untracked local cruft
    # (__pycache__, a local .venv) and would make the gap report depend on working-tree state.
    r = subprocess.run(["git", "ls-files"], cwd=audit.ROOT, capture_output=True, text=True)
    if r.returncode != 0:  # fail loud: an empty set would masquerade as "no gaps" (a false clean)
        raise RuntimeError(f"git ls-files failed in {audit.ROOT}: {r.stderr.strip() or 'non-zero exit'}")
    hub_files = set(r.stdout.splitlines())
    gaps = sorted(n["path"] for n in tree["tree"]
                  if n.get("type") == "blob" and n["path"] in hub_files and n["path"] not in listed)
    return slug, gaps


def load_ledger():
    """The curated disposition ledger, empty if absent (each verbatim divergence and manifest gap then reads as untriaged).

    Normalized so a malformed file (non-object root, or a non-array dispositions/gaps) degrades to an empty
    section rather than crashing render_report. validate.py reports the malformation loudly in CI.
    """
    try:
        led = audit.load("spec/divergences.json")
    except FileNotFoundError:
        led = {}
    if not isinstance(led, dict):
        led = {}
    for key in ("dispositions", "gaps"):
        if not isinstance(led.get(key), list):
            led[key] = []
    return led


def _fmt(repos):
    return ", ".join(sorted(repos)) if repos else "-"


def render_report(spreads, promote, gaps, ledger):
    """Join the live passes against the curated ledger into the checked-in burn-down markdown.

    A recorded disposition still matching a live divergence is a burn-down row. A live divergence (a
    verbatim hand-modification, or a manifest gap) with no disposition reads UNTRIAGED. A disposition is
    resolved only when every recorded repo now matches the canonical - a repo that went unavailable
    (deleted, renamed, or too large to fetch inline) is unverified, not resolved, so it stays on the row.
    Verbatim stale copies are the mechanical re-vendor list (the audit already flags them), kept separate.
    """
    spread_by_path = {e["path"]: sp for e, sp in spreads if sp is not None}

    def buckets(path):
        # (still divergent, confirmed match, unavailable) repo sets for a unit. Unavailable is held apart
        # from match: an absent copy cannot confirm a divergence was fixed.
        sp = spread_by_path.get(path)
        if not sp:
            return set(), set(), set()
        return set(sp["differs"]) | set(sp["stale"]), set(sp["match"]), set(sp["unavailable"])

    dispositions = ledger.get("dispositions", [])
    gap_entries = ledger.get("gaps", [])
    gap_disp = {g["path"]: g for g in gap_entries}
    covered = {}  # path -> repos that carry a disposition (to find the untriaged remainder)
    for d in dispositions:
        covered.setdefault(d["path"], set()).update(d["repos"])

    # Untriaged: verbatim hand-modifications with no disposition, and live gaps with no gap disposition.
    # Verbatim-only by design: an intent unit's byte diff is expected (judged by meaning), so only a verbatim
    # hand-modification with no recorded disposition is a genuine anomaly worth surfacing.
    untriaged_files = []
    for e, sp in spreads:
        if sp is None or e["fidelity"] != "verbatim":
            continue
        rest = sorted(set(sp["differs"]) - covered.get(e["path"], set()))
        if rest:
            untriaged_files.append((e["path"], rest))
    untriaged_gaps = [g for g in gaps if g not in gap_disp]

    order = ["re-vendor", "upstream-candidate", "investigate", "track", "accepted"]
    by_disp = {k: [] for k in order}
    resolved = []
    for d in dispositions:
        dset = set(d["repos"])
        div, matched, unavail = buckets(d["path"])
        live = sorted(dset & div)
        gone = sorted(dset & matched)
        unk = sorted(dset & unavail)
        if not live and not unk:  # every recorded repo now matches the canonical
            resolved.append(d)
            continue
        by_disp.setdefault(d["disposition"], []).append((d, live, gone, unk))
    for g in gap_entries:  # a gap disposition stays live while the file is still an untracked gap
        if g["path"] in gaps:
            by_disp.setdefault(g["disposition"], []).append((g, None, None, None))

    out = []
    w = out.append
    w("# Fleet divergence report")
    w("")
    w("Generated by `python3 spec/fidelity_honesty.py --report` - do not hand-edit. Curate dispositions in [`spec/divergences.json`][ledger] and regenerate. Each row reflects a repo's ground-truth branch at generation time. Git dates this file.")
    w("")

    w("## Burn-down")
    w("")
    if not any(by_disp[k] for k in by_disp):
        w("_Nothing recorded and live._")
        w("")
    for k in order:
        rows = by_disp.get(k, [])
        if not rows:
            continue
        w(f"### {k}")
        w("")
        for entry, live, gone, unk in rows:
            trk = f" (tracking: {entry['tracking']})" if entry.get("tracking") else ""
            if live is None:  # a manifest-gap disposition (not repo-scoped)
                w(f"- **{entry['path']}** (manifest gap){trk} - {entry['reason']}")
            else:
                extra = f" _(recorded {_fmt(gone)} now resolved)_" if gone else ""
                extra += f" _(unavailable, unverified: {_fmt(unk)})_" if unk else ""
                w(f"- **{entry['path']}** - {_fmt(live) if live else '(none live)'}{trk}{extra} - {entry['reason']}")
        w("")

    w("## Untriaged - add a disposition to `spec/divergences.json`")
    w("")
    if not untriaged_files and not untriaged_gaps:
        w("_None - every live divergence has a recorded disposition._")
        w("")
    else:
        for path, repos in untriaged_files:
            w(f"- **{path}** - hand-modified in {_fmt(repos)} (verbatim canonical)")
        for g in untriaged_gaps:
            w(f"- **{g}** - carried by the reference adopter but not in the manifest")
        w("")

    w("## Mechanical re-vendor (verbatim stale copies)")
    w("")
    w("A past hub revision, not the current canonical - the audit already flags these as DRIFT. Copy the current file down. No judgment needed.")
    w("")
    stale_rows = [(e["path"], sp["stale"]) for e, sp in spreads
                  if sp is not None and e["fidelity"] == "verbatim" and sp["stale"]]
    if not stale_rows:
        w("_None._")
    for path, repos in stale_rows:
        w(f"- **{path}** ({len(repos)}): {_fmt(repos)}")
    w("")

    w("## Promote candidates (intent uniform -> verbatim)")
    w("")
    if not promote:
        w("_None - no intent unit is currently fleet-uniform with the canonical._")
    for e, sp in promote:
        w(f"- **{e['path']}**: {len(sp['match'])} match, {len(sp['stale'])} stale, 0 hand-modified")
    w("")

    if resolved:
        w("## Resolved (recorded but no longer live - remove from the ledger)")
        w("")
        for d in resolved:
            w(f"- **{d['path']}** - {_fmt(d['repos'])} ({d['disposition']})")
        w("")

    # Reference-link definitions live at the bottom of the document.
    w("[ledger]: ../spec/divergences.json")
    return "\n".join(out).rstrip() + "\n"


def main():
    argv = sys.argv[1:]
    report_mode = "--report" in argv
    positional = [a for a in argv if not a.startswith("-")]
    ref_repo = positional[0] if positional else REF_ADOPTER
    spec = {
        "registry": audit.load("registry/repos.json"),
        "files": audit.load("spec/files.json"),
    }
    spreads, promote, mislabel = fidelity_pass(spec)
    slug, gaps = manifest_gap_pass(spec, ref_repo)

    if report_mode:
        content = render_report(spreads, promote, gaps, load_ledger())
        # CRLF to match the fleet default (reports/*.md is CRLF). Write bytes so the local platform does not
        # re-translate. Git dates the file, so no timestamp is embedded (it would churn every regeneration).
        (audit.ROOT / REPORT_PATH).write_bytes(content.replace("\n", "\r\n").encode("utf-8"))
        print(f"Wrote {REPORT_PATH} ({len(content.splitlines())} lines)")
        return 0

    print("== Per-unit fleet spread (ground-truth branch per repo) ==")
    print("   fidelity path :: match / stale / differs / unavailable (absent or non-inline)")
    for e, spread in spreads:
        if spread is None:
            print(f"   {e.get('fidelity'):8} {e['path']} :: canonical unreadable at hub - skipped")
            continue
        print(f"   {e['fidelity']:8} {e['path']} :: "
              f"{len(spread['match'])} / {len(spread['stale'])} / {len(spread['differs'])} / {len(spread['unavailable'])}")

    print("\n== INTENT units with no divergent copy (verbatim-appropriate) -> candidates to promote to VERBATIM ==")
    print("   (>=1 confirmed match, 0 hand-modified. Any stale/unavailable copy is shown per unit and would")
    print("    re-vendor under verbatim - the drift intent cannot catch)")
    if not promote:
        print("   none")
    for e, spread in promote:
        print(f"   {e['path']}: {len(spread['match'])} match, {len(spread['stale'])} stale, "
              f"{len(spread['unavailable'])} unavailable, 0 differ")

    print("\n== VERBATIM units with NON-stale downstream divergence (mis-label or real drift) ==")
    if not mislabel:
        print("   none")
    for e, spread in mislabel:
        print(f"   {e['path']}: differs in {', '.join(spread['differs'])}")

    print(f"\n== Manifest gap: files carried by {ref_repo} + present at the hub but NOT in the manifest ==")
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

#!/usr/bin/env python3
"""Workflow-reuse measurement: how much of the fleet's GitHub Actions YAML is a copy of a hub canonical.

Read-only and owner-run. The measurement reads the live fleet, so it is not wired into CI beyond the offline
--selftest CI runs beside the audit engine's. Reuses spec/audit.py's fleet machinery (gh, normalize, registry).

The hub hosts a standard workflow once, as a `workflow_call` reusable workflow a downstream repo reaches by a
pinned `uses:`, and a downstream repo carries only a caller stub plus a composite-action hook for what is
genuinely its own. This tool measures the distance from that model, so the burn-down is a number rather than
an impression:

  1. Every `.github/workflows/*.yml` in every cataloged repo, read from its ground-truth branch, is inventoried
     and compared line by line against the hub canonical of the same name (the hub's own workflow or the
     catalog snippet), after the same normalization the verbatim engine applies (line endings, action pins,
     job needs), so a Dependabot bump is not counted as divergence.
  2. Copies of one canonical are clustered by similarity to a cluster leader, which answers the question that
     decides whether a job is worth hosting once: how many genuinely distinct variants of it the fleet runs.
  3. A file that reaches a hub reusable workflow or a hub composite action is counted as a caller, which is
     the state every carried copy converges to.

The output is a fleet total (files, lines, the share of lines byte-identical to a canonical, callers), a
per-workflow table, a per-repo table, and the repo-invented workflows no canonical names.

Usage: python3 spec/workflow_reuse.py [--report] [--selftest]
"""

import base64
import difflib
import re
import sys

import audit  # sibling, import-safe (its main is guarded)

REPORT_PATH = "reports/workflow-reuse.md"  # the generated, checked-in burn-down report (--report)
WORKFLOW_DIR = ".github/workflows"
# Two copies at or above this ratio are one variant.
# 0.85 rather than higher, because a copy that differs only in its per-repo target list or its comment wording is the same variant for the purpose of hosting the job once.
CLUSTER_THRESHOLD = 0.85
_USES = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)


def canonical_texts():
    """The hub's canonical workflows by filename, its own orchestrator set plus the catalog snippets.

    Both trees are read from the hub checkout, so the comparison is against the hub commit the tool runs from,
    which the report names.
    """
    canon = {}
    for rel in (WORKFLOW_DIR, "catalog/snippets/workflows"):
        for p in sorted((audit.ROOT / rel).glob("*.yml")):
            canon.setdefault(p.name, p.read_text(encoding="utf-8", errors="replace"))
    return canon


def fetch_dir(slug, path, ref):
    """The filenames under a directory on a ref, empty where the directory is absent.

    A 404 here is a repo that carries no workflows, which is a real state rather than an unread one, so it
    counts as zero files. A failure other than 404 raises inside audit.gh, so nothing is silently skipped.
    """
    listing = audit.gh(f"repos/{slug}/contents/{path}?ref={ref}", ok404=True)
    if not isinstance(listing, list):
        return []
    return sorted(n["name"] for n in listing if n.get("type") == "file")


def fetch(slug, path, ref):
    """Decoded downstream file content, or None if absent / not inline."""
    content = audit.gh(f"repos/{slug}/contents/{path}?ref={ref}", ok404=True)
    if content is None or content.get("encoding") != "base64":
        return None
    return base64.b64decode(content["content"]).decode("utf-8", "replace")


def norm_lines(text):
    return audit.normalize(text).splitlines()


def compare(text, canon_text):
    """(lines, identical lines, similarity ratio) of a copy against its canonical, after normalization."""
    a, b = norm_lines(canon_text), norm_lines(text)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    identical = sum(block.size for block in matcher.get_matching_blocks())
    return len(b), identical, round(matcher.ratio(), 2)


def hub_reach(text, hub_slug):
    """The hub paths a workflow reaches through `uses:`, reusable workflows and composite actions alike."""
    prefix = f"{hub_slug}/"
    return sorted({m.group(1) for m in _USES.finditer(text) if m.group(1).startswith(prefix)})


def cluster(copies):
    """Leader clusters of {repo: text} at CLUSTER_THRESHOLD, each a sorted repo list.

    Each copy, in name order, joins the first cluster whose leader (its first member) it matches at the
    threshold, else it starts a cluster. So a member is guaranteed similar to its leader rather than to every
    other member. Leader clustering rather than an exhaustive partition on purpose: the count of variants is
    what the report needs, and a stricter partition would only split a borderline pair into two clusters that
    read the same to a maintainer.
    """
    lines = {r: norm_lines(t) for r, t in copies.items()}
    clusters = []
    for repo in sorted(copies):
        for members in clusters:
            ratio = difflib.SequenceMatcher(
                None, lines[members[0]], lines[repo], autojunk=False
            ).ratio()
            if ratio >= CLUSTER_THRESHOLD:
                members.append(repo)
                break
        else:
            clusters.append([repo])
    return clusters


def measure(registry, canon, hub_slug, reader=fetch, lister=fetch_dir):
    """Inventory every cataloged repo's workflows and compare each against its canonical.

    Returns per-file rows, the files that were listed but could not be read inline, and the downstream repos
    carrying no workflow at all. An unreadable file is named rather than dropped, since a dropped file and an
    absent one print the same, and an empty repo is named so the repo count is a count of what was measured.
    """
    rows, unreadable, empty = [], [], []
    for entry in registry["repos"]:
        if entry.get("status") != "cataloged":
            continue
        name = entry["name"]
        slug = audit.repo_slug(entry)
        ref = entry.get("groundTruthBranch", "main")
        files = [f for f in lister(slug, WORKFLOW_DIR, ref) if f.endswith((".yml", ".yaml"))]
        if not files and name != audit.HUB_NAME:
            empty.append(name)
        for fname in files:
            text = reader(slug, f"{WORKFLOW_DIR}/{fname}", ref)
            if text is None:
                unreadable.append(f"{name}:{fname}")
                continue
            row = {
                "repo": name,
                "hub": name == audit.HUB_NAME,
                "file": fname,
                "text": text,
                "reach": hub_reach(text, hub_slug),
                "canonical": fname in canon,
            }
            if fname in canon:
                row["lines"], row["identical"], row["ratio"] = compare(text, canon[fname])
            else:
                row["lines"], row["identical"], row["ratio"] = len(norm_lines(text)), 0, None
            rows.append(row)
    return rows, unreadable, empty


def summarize(rows):
    """Fleet totals over the downstream rows (the hub's own copies are the canonicals and are excluded)."""
    down = [r for r in rows if not r["hub"]]
    lines = sum(r["lines"] for r in down)
    identical = sum(r["identical"] for r in down)
    return {
        "files": len(down),
        "lines": lines,
        "identical": identical,
        "identical_pct": round(100 * identical / lines) if lines else 0,
        "canonical_named": sum(1 for r in down if r["canonical"]),
        "callers": sum(1 for r in down if r["reach"]),
        "repos": len({r["repo"] for r in down}),
    }


def per_workflow(rows):
    """One row per canonical filename over the downstream copies: copies, lines, identical, clusters, callers."""
    by_name = {}
    for r in rows:
        if r["hub"] or not r["canonical"]:
            continue
        by_name.setdefault(r["file"], []).append(r)
    table = []
    for fname, copies in sorted(by_name.items(), key=lambda kv: -sum(x["lines"] for x in kv[1])):
        clusters = cluster({r["repo"]: r["text"] for r in copies})
        table.append(
            {
                "file": fname,
                "copies": len(copies),
                "lines": sum(r["lines"] for r in copies),
                "identical": sum(r["identical"] for r in copies),
                "clusters": clusters,
                "callers": sorted(r["repo"] for r in copies if r["reach"]),
            }
        )
    return table


def _fmt(items):
    return ", ".join(items) if items else "-"


def render_report(rows, unreadable, empty, hub_sha):
    total = summarize(rows)
    table = per_workflow(rows)
    out = []
    w = out.append
    w("# Fleet workflow reuse report")
    w("")
    w(
        f'Generated by `python3 spec/workflow_reuse.py --report` at hub `{hub_sha}` - do not hand-edit. Each row reads a repo\'s ground-truth branch at generation time and compares it against the hub canonical of the same name after line-ending, action-pin, and job-needs normalization, per [`spec/fidelity-model.md`][fidelity-model] "Normalization". Git dates this file. The target model and the migration phases are in [`docs/reusable-workflows.md`][reusable-workflows].'
    )
    w("")
    w("## Fleet Total")
    w("")
    w(
        f"- **{total['files']} workflow files, {total['lines']:,} lines** across {total['repos']} downstream repos, {total['canonical_named']} of them named for a hub canonical."
        + (f" No workflow at all in {_fmt(empty)}." if empty else "")
    )
    w(
        f"- **{total['identical']:,} lines ({total['identical_pct']}%) are byte-identical to a hub canonical** after normalization, which is the confirmed duplication. The rest is mostly a per-repo edit of the same canonical rather than independent code."
    )
    w(
        f"- **Files reaching a hub reusable workflow or composite action through a pinned `uses:`: {total['callers']}.** That is the state every carried copy converges to, so this number rises and the two above fall as the migration lands."
    )
    w("")
    w("## Per Workflow")
    w("")
    w(
        f"Downstream copies of each hub canonical. A variant is a cluster of copies each at or above {CLUSTER_THRESHOLD} similarity to the cluster's first member, so the cluster count is how many distinct shapes of one workflow the fleet runs today. Callers are the copies that already reach the hub rather than carrying the job bodies."
    )
    w("")
    w("| File | Copies | Lines | Identical to hub | Variants | Callers |")
    w("| --- | --- | --- | --- | --- | --- |")
    for t in table:
        w(
            f"| `{t['file']}` | {t['copies']} | {t['lines']:,} | {t['identical']:,} | {len(t['clusters'])} | {len(t['callers'])} |"
        )
    w("")
    w("### Variant Members")
    w("")
    w(
        "Each variant names the repos whose copies cluster together, so a hub task's inputs and hooks can be designed against the shapes that exist rather than against the canonical alone."
    )
    w("")
    for t in table:
        w(f"- `{t['file']}`")
        for c in t["clusters"]:
            w(f"  - {len(c)}: {_fmt(c)}")
    w("")
    w("## Per Repo")
    w("")
    w("| Repo | Files | Lines | Identical to hub | Callers | Repo-local files |")
    w("| --- | --- | --- | --- | --- | --- |")
    by_repo = {}
    for r in rows:
        if not r["hub"]:
            by_repo.setdefault(r["repo"], []).append(r)
    for repo, rs in sorted(by_repo.items()):
        local = sorted(f"`{r['file']}`" for r in rs if not r["canonical"])
        w(
            f"| {repo} | {len(rs)} | {sum(r['lines'] for r in rs):,} | {sum(r['identical'] for r in rs):,} | {sum(1 for r in rs if r['reach'])} | {_fmt(local)} |"
        )
    w("")
    w("## Repo-Local Workflows")
    w("")
    w(
        "A workflow no hub canonical names. Each is either genuinely repo-specific, and stays, or a candidate for a hub task with a hook, and the design doc lists which."
    )
    w("")
    local_rows = [r for r in rows if not r["hub"] and not r["canonical"]]
    if not local_rows:
        w("_None._")
    for r in sorted(local_rows, key=lambda x: (x["repo"], x["file"])):
        w(f"- **{r['repo']}** `{r['file']}` ({r['lines']} lines)")
    w("")
    if unreadable:
        w("## Unreadable")
        w("")
        w(
            f"A listed workflow could not be read inline for {_fmt(unreadable)}, so those are named rather than counted as absent."
        )
        w("")
    w("<!-- Repo -->")
    w("")
    w("[fidelity-model]: ../spec/fidelity-model.md")
    w("[reusable-workflows]: ../docs/reusable-workflows.md")
    return "\n".join(out).rstrip() + "\n"


def hub_sha():
    import subprocess

    r = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=audit.ROOT,
        check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def _selftest():
    """Offline proof of the pure functions, so a regression in the measurement fails CI rather than a sweep."""
    canon = (
        "name: A\non:\n  workflow_call:\njobs:\n  x:\n    steps:\n      - uses: actions/checkout@"
        + "a" * 40
        + " # v7.0.0\n"
    )
    same_pin_bumped = canon.replace("a" * 40 + " # v7.0.0", "b" * 40 + " # v7.0.1")
    crlf = canon.replace("\n", "\r\n")
    lines, identical, ratio = compare(same_pin_bumped, canon)
    assert (lines, identical, ratio) == (7, 7, 1.0), (lines, identical, ratio)
    assert compare(crlf, canon)[1:] == (7, 1.0)
    edited = canon.replace("name: A", "name: B").replace("  x:", "  y:")
    l2, i2, r2 = compare(edited, canon)
    assert l2 == 7 and i2 == 5 and r2 < 1.0, (l2, i2, r2)

    hub = "acme/hub"
    caller = (
        f"jobs:\n  a:\n    uses: {hub}/.github/workflows/merge-bot-task.yml@{'c' * 40} # 2.0.1\n"
    )
    assert hub_reach(caller, hub) == [f"{hub}/.github/workflows/merge-bot-task.yml@{'c' * 40}"]
    assert hub_reach(canon, hub) == []
    local_uses = (
        "steps:\n  - uses: ./.github/actions/x\n  - uses: acme/other/.github/actions/y@"
        + "d" * 40
        + "\n"
    )
    assert hub_reach(local_uses, hub) == []

    big = "\n".join(f"line {i}" for i in range(40)) + "\n"
    near = big.replace("line 3\n", "line three\n")
    far = "\n".join(f"other {i}" for i in range(40)) + "\n"
    assert cluster({"r1": big, "r2": near, "r3": far}) == [["r1", "r2"], ["r3"]]

    registry = {
        "repos": [
            {
                "name": audit.HUB_NAME,
                "status": "cataloged",
                "url": f"https://x/acme/{audit.HUB_NAME}",
            },
            {"name": "One", "status": "cataloged", "url": "https://x/acme/One"},
            {
                "name": "Two",
                "status": "cataloged",
                "url": "https://x/acme/Two",
                "groundTruthBranch": "develop",
            },
            {"name": "Skip", "status": "planned", "url": "https://x/acme/Skip"},
            {"name": "Gone", "status": "cataloged", "url": "https://x/acme/Gone"},
        ]
    }
    canon_map = {"a.yml": canon}
    trees = {
        audit.HUB_NAME: ["a.yml"],
        "One": ["a.yml", "local.yml", "notes.md"],
        "Two": ["a.yml", "big.yml"],
    }
    texts = {
        (audit.HUB_NAME, "a.yml"): canon,
        ("One", "a.yml"): same_pin_bumped,
        ("One", "local.yml"): "name: L\n",
        ("Two", "a.yml"): caller,
    }
    seen_refs = {}

    def lister(slug, path, ref):
        name = slug.split("/")[-1]
        seen_refs[name] = ref
        return trees.get(name, [])

    def reader(slug, path, ref):
        return texts.get((slug.split("/")[-1], path.split("/")[-1]))

    rows, unreadable, empty = measure(registry, canon_map, hub, reader=reader, lister=lister)
    assert unreadable == ["Two:big.yml"], unreadable
    assert empty == ["Gone"], empty
    assert seen_refs["Two"] == "develop" and seen_refs["One"] == "main"
    total = summarize(rows)
    assert total["files"] == 3 and total["canonical_named"] == 2 and total["callers"] == 1, total
    assert total["repos"] == 2 and total["lines"] == 7 + 1 + 3, total
    assert total["identical"] == 8, total  # 7 from One plus the shared `jobs:` line of the caller
    table = per_workflow(rows)
    assert [t["file"] for t in table] == ["a.yml"] and table[0]["copies"] == 2
    assert table[0]["callers"] == ["Two"] and len(table[0]["clusters"]) == 2, table
    report = render_report(rows, unreadable, empty, "abc1234")
    assert "| `a.yml` | 2 | 10 | 8 | 2 | 1 |" in report, report
    assert "- `a.yml`\n  - 1: One\n  - 1: Two\n" in report, report
    assert "- **One** `local.yml` (1 lines)" in report
    assert "Unreadable" in report and "Two:big.yml" in report
    assert "No workflow at all in Gone." in report
    assert "[reusable-workflows]: ../docs/reusable-workflows.md" in report
    assert not any(ord(ch) > 127 for ch in report), "report is ASCII"
    print("workflow_reuse selftest OK")
    return 0


def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:
        return _selftest()
    registry = audit.load("registry/repos.json")
    hub_entry = next(
        (r for r in registry["repos"] if r.get("name") == audit.HUB_NAME),
        {"name": audit.HUB_NAME},
    )
    hub_slug = audit.repo_slug(hub_entry)
    rows, unreadable, empty = measure(registry, canonical_texts(), hub_slug)
    if "--report" in argv:
        content = render_report(rows, unreadable, empty, hub_sha())
        # LF matches the fleet default, since reports/*.md is LF.
        # Bytes are written so the local platform does not re-translate them.
        (audit.ROOT / REPORT_PATH).write_bytes(content.encode("utf-8"))
        print(f"Wrote {REPORT_PATH} ({len(content.splitlines())} lines)")
        return 0
    total = summarize(rows)
    print(
        f"{total['files']} downstream workflow files, {total['lines']} lines, "
        f"{total['identical']} ({total['identical_pct']}%) identical to a hub canonical, "
        f"{total['callers']} reach the hub"
    )
    for t in per_workflow(rows):
        print(
            f"   {t['file']:40s} copies={t['copies']:2d} lines={t['lines']:5d} "
            f"identical={t['identical']:5d} variants={len(t['clusters'])} callers={len(t['callers'])}"
        )
    if unreadable:
        print(f"   unreadable: {', '.join(unreadable)}")
    if empty:
        print(f"   no workflows: {', '.join(empty)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

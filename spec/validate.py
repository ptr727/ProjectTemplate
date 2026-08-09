#!/usr/bin/env python3
"""Validate the registry and spec cross-references (stdlib only).

Checks that every cataloged repo classifies against the spec: its types resolve,
its publish mechanisms are recognized, and its secrets are consistent with
spec/secrets.json. Exits non-zero on any failure. This is the classification
dry-run the CI lint job runs; it needs no third-party packages.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Scope-selector vocabularies, per spec/scope-model.md, kept in sync with the $defs in registry/repos.schema.json.
# The four namespaces, meaning project types plus these three, must stay disjoint so a flat appliesTo token set in spec/files.json is unambiguous.
WORKFLOW_MODELS = ("release", "operational")
RELEASE_TRIGGERS = ("two-phase", "publish-on-merge", "dispatch-only", "none")
CONSUMER_MODELS = ("push", "pull")
# How faithfully a carried unit is checked, per spec/fidelity-model.md, defaulting to presence.
FIDELITIES = ("presence", "intent", "verbatim", "interface")
# The keys an interface unit's `contract` may carry (kept in sync with files.schema.json).
CONTRACT_KEYS = {"requiredJobKeys", "requiredCheckName", "artifactNameToken", "requireTokensInJob", "forbidTokensInJob", "verbatimJobs"}


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def is_str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def main():
    errors = []
    repos = load("registry/repos.json")
    types = load("spec/project-types.json")
    secrets = load("spec/secrets.json")

    for key, obj, fname in [
        ("repos", repos, "repos.json"),
        ("types", types, "project-types.json"),
        ("baseline", secrets, "secrets.json"),
        ("mechanisms", secrets, "secrets.json"),
        ("targetMechanisms", secrets, "secrets.json"),
    ]:
        if key not in obj:
            errors.append(f"{fname}: missing required key '{key}'")
    if errors:
        print("Spec validation FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    known_types = set(types["types"])
    target_mech = secrets["targetMechanisms"]
    mechanisms = secrets["mechanisms"]

    # CI runs no JSON-schema validation, so shape-check secrets.json here to fail with a clear message rather than crash the cross-reference loops below.
    def check_secret_set(label, entry, need_kind):
        if not isinstance(entry, dict):
            errors.append(f"secrets.json: {label} is not an object")
            return
        for field in ("requires", "forbids"):
            val = entry.get(field)
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                errors.append(f"secrets.json: {label} '{field}' must be an array of strings")
        if need_kind:
            if entry.get("kind") not in ("oidc", "static-secret"):
                errors.append(f"secrets.json: {label} has a missing or invalid kind")
            elif entry.get("kind") == "oidc" and not entry.get("forbids"):
                errors.append(f"secrets.json: oidc {label} forbids no static credential")

    check_secret_set("baseline", secrets.get("baseline"), need_kind=False)

    # The README model is indexed directly by spec/audit.py, so a key that is absent or the wrong type crashes the audit mid-run rather than reporting.
    # The schema marks each one required, but CI runs no JSON-schema validation, so the guard that actually runs is this one.
    # Checked as a set rather than one key at a time, since guarding only the keys a review happened to name is how the other four came to be unguarded.
    readme_model = load("spec/readme-sections.json")
    if not isinstance(readme_model, dict):
        errors.append("readme-sections.json: top level is not an object")
    else:
        for key, want in (("sections", list), ("shieldClasses", list), ("linkGroups", list),
                          ("linkNaming", list), ("canonicalLinks", list), ("distribution", dict)):
            value = readme_model.get(key)
            if not isinstance(value, want) or not value:
                errors.append(f"readme-sections.json: '{key}' must be a non-empty {'array' if want is list else 'object'}, and spec/audit.py indexes it directly")
        # The distribution prefixes are what tell a repo's own URLs from a third party's.
        # Their absence fails open rather than loud: link_kind would classify every own URL as external, canonical naming would quietly stop being enforced, and the audit would still report green.
        prefixes = readme_model.get("distribution", {}).get("urlPrefixes") if isinstance(readme_model.get("distribution"), dict) else None
        if not is_str_list(prefixes) or not prefixes:
            errors.append("readme-sections.json: 'distribution.urlPrefixes' must be a non-empty array of strings, or the link audit stops distinguishing this repo's URLs from a third party's and silently passes")
        else:
            # Every prefix, not merely one of them.
            # A broad entry added beside a valid one would pass an any() guard while making link_kind read a third party's URL as this repo's own, which is the failure the guard exists to stop.
            loose = [p for p in prefixes if "{slug}" not in p and "{owner}" not in p]
            if loose:
                errors.append(f"readme-sections.json: 'distribution.urlPrefixes' entry {loose[0]!r} carries neither {{slug}} nor {{owner}}, so it is not repo-scoped and would match another owner's URLs")
        # A canonicalLinks entry naming a destination it cannot match is a name nothing enforces, and audit.py raises on it mid-run rather than reporting.
        for c in readme_model.get("canonicalLinks", []) if isinstance(readme_model.get("canonicalLinks"), list) else []:
            if isinstance(c, dict) and "repoPath" not in c and "match" not in c:
                errors.append(f"readme-sections.json: canonicalLinks entry '{c.get('name')}' carries neither 'repoPath' nor 'match', so it can match no URL")

    # CI runs no JSON-schema validation, so shape-check the shared tool catalog here.
    # A duplicate name is the failure worth catching: the audit keys on it, so the second entry silently shadows the first and half the fleet is measured against a description nobody can see.
    # The top level is read defensively rather than assumed: a malformed file (a bare array, say) would raise
    # AttributeError off .get and crash the run, which is the opposite of what shape-checking here is for.
    catalog = load("spec/third-party-tools.json")
    if not isinstance(catalog, dict):
        # One diagnostic, naming the outermost thing that is wrong.
        # Reporting the missing 'tools' as well would describe a consequence of this as if it were a second defect.
        errors.append("third-party-tools.json: top level is not an object")
    elif not isinstance(catalog.get("tools"), list) or not catalog["tools"]:
        errors.append("third-party-tools.json: 'tools' must be a non-empty array")
    else:
        tools = catalog["tools"]
        seen = set()
        for t in tools:
            name = t.get("name") if isinstance(t, dict) else None
            if not isinstance(t, dict) or not all(isinstance(t.get(f), str) and t.get(f) for f in ("name", "link", "description")):
                errors.append(f"third-party-tools.json: entry {name or t!r} needs a non-empty name, link and description")
                continue
            if name.lower() in seen:
                errors.append(f"third-party-tools.json: duplicate tool name '{name}' - the audit keys on it, so the second entry would shadow the first")
            seen.add(name.lower())
            desc = t["description"]
            if not (desc[0].isupper() and desc.endswith(".")):
                errors.append(f"third-party-tools.json: '{name}' description {desc!r} is not a sentence - open with a capital and close with a full stop")
        names = [t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]
        if names != sorted(names, key=str.lower):
            errors.append("third-party-tools.json: 'tools' is not sorted by name, which is how a reader finds an entry to copy")
    if not isinstance(mechanisms, dict):
        errors.append("secrets.json: 'mechanisms' is not an object")
    else:
        for mname, m in mechanisms.items():
            check_secret_set(f"mechanism '{mname}'", m, need_kind=True)
    if not isinstance(target_mech, dict):
        errors.append("secrets.json: 'targetMechanisms' is not an object")
    else:
        for t, v in target_mech.items():
            if not (v is None or isinstance(v, str)):
                errors.append(f"secrets.json: targetMechanisms['{t}'] must be a mechanism name or null")
    feature_mech = secrets.get("featureMechanisms", {})
    if not isinstance(feature_mech, dict):
        errors.append("secrets.json: 'featureMechanisms' is not an object")
    else:
        for f, v in feature_mech.items():
            if not (v is None or isinstance(v, str)):
                errors.append(f"secrets.json: featureMechanisms['{f}'] must be a mechanism name or null")
    type_mech = secrets.get("typeMechanisms", {})
    if not isinstance(type_mech, dict):
        errors.append("secrets.json: 'typeMechanisms' is not an object")
    else:
        for t, v in type_mech.items():
            if not (v is None or isinstance(v, str)):
                errors.append(f"secrets.json: typeMechanisms['{t}'] must be a mechanism name or null")
    if errors:
        print("Spec validation FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    # The defaults for workflowModel and releaseTrigger feed configure.sh's fallback and selector resolution.
    # An invalid value there breaks the apply or scopes wrong while every per-repo entry still validates, so check them once.
    reg_defaults = repos.get("defaults", {})
    default_model = reg_defaults.get("workflowModel")
    if default_model is not None and default_model not in WORKFLOW_MODELS:
        errors.append(f"defaults.workflowModel '{default_model}' invalid (expected {' or '.join(WORKFLOW_MODELS)})")
    default_trigger = reg_defaults.get("releaseTrigger")
    if default_trigger is not None and default_trigger not in RELEASE_TRIGGERS:
        errors.append(f"defaults.releaseTrigger '{default_trigger}' invalid (expected one of {', '.join(RELEASE_TRIGGERS)})")

    for i, repo in enumerate(repos["repos"]):
        if not isinstance(repo, dict):
            errors.append(f"repo #{i} is not an object")
            continue
        name = repo.get("name", f"#{i}")
        status = repo.get("status")
        if status is None:
            errors.append(f"{name}: missing 'status'")
            continue
        if status == "backlog":
            if not repo.get("classificationPending"):
                errors.append(f"{name}: backlog repo without classificationPending")
            continue
        if status != "cataloged":
            errors.append(f"{name}: unknown status '{status}'")
            continue

        repo_types = repo.get("types", [])
        if not repo_types:
            errors.append(f"{name}: cataloged repo has no types (add types or mark it backlog)")
        for t in repo_types:
            if t not in known_types:
                errors.append(f"{name}: type '{t}' not defined in project-types.json")
        # A declared profile must name one of the repo's types and a profile that type allows (spec/type-model.md).
        # CI runs no JSON-schema validation, so guard the shape here rather than crash on .items().
        profiles_decl = repo.get("profiles", {})
        if not isinstance(profiles_decl, dict):
            errors.append(f"{name}: profiles must be an object mapping a type to its profile")
            profiles_decl = {}
        for tname, prof in profiles_decl.items():
            allowed = types["types"].get(tname, {}).get("profiles", [])
            if tname not in repo_types:
                errors.append(f"{name}: profile declared for '{tname}', not one of the repo's types")
            elif tname in known_types and prof not in allowed:
                errors.append(f"{name}: type '{tname}' profile '{prof}' not in its allowed profiles {allowed or '[]'}")

        model = repo.get("workflowModel")
        if model is not None and model not in WORKFLOW_MODELS:
            errors.append(f"{name}: workflowModel '{model}' invalid (expected {' or '.join(WORKFLOW_MODELS)})")

        # The releaseTrigger field is a scope selector, per spec/scope-model.md, so an invalid value would silently fail to match any releaseTrigger-scoped section rather than error.
        trigger = repo.get("releaseTrigger")
        if trigger is not None and trigger not in RELEASE_TRIGGERS:
            errors.append(f"{name}: releaseTrigger '{trigger}' invalid (expected one of {', '.join(RELEASE_TRIGGERS)})")

        # The consumerModel field is a scope selector, per spec/scope-model.md, so a cataloged repo must declare it.
        # Otherwise a push-scoped or pull-scoped section would fail open on that repo, never matching.
        cm = repo.get("consumerModel")
        if cm not in CONSUMER_MODELS:
            errors.append(f"{name}: consumerModel '{cm}' invalid or missing (expected {' or '.join(CONSUMER_MODELS)})")

        eol = repo.get("lineEndings")
        if eol is not None and eol not in ("lf", "crlf"):
            errors.append(f"{name}: lineEndings '{eol}' invalid (expected lf or crlf)")
        # An operational repo's endings follow the consuming app's platform, so they must be declared, where a release repo omits the field and takes the fleet CRLF default.
        # Resolve the effective model the way configure.sh does, from the repo, then the defaults, then release.
        # The requirement then holds even where a repo relies on an operational defaults.workflowModel rather than setting its own.
        effective_model = model or default_model or "release"
        if effective_model == "operational" and eol is None:
            errors.append(f"{name}: operational repo must declare lineEndings (lf or crlf)")

        required = set(repo.get("requiredSecrets", []))
        for pub in repo.get("publish", []):
            if not isinstance(pub, dict) or "target" not in pub or "mechanism" not in pub:
                errors.append(f"{name}: publish entry missing 'target'/'mechanism'")
                continue
            target, mech = pub["target"], pub["mechanism"]
            if target not in target_mech:
                errors.append(f"{name}: publish target '{target}' unknown")
                continue
            mech_key = target_mech[target]
            if mech_key is None:
                continue
            if mech_key not in mechanisms:
                errors.append(f"{name}: target '{target}' maps to undefined mechanism '{mech_key}'")
                continue
            spec_mech = mechanisms[mech_key]
            # A docker or static-secret target must carry its required secrets.
            for req in spec_mech.get("requires", []):
                if req not in required:
                    errors.append(f"{name}: {target} requires secret '{req}' (missing)")
            # An oidc mechanism must not carry a forbidden static key.
            for bad in spec_mech.get("forbids", []):
                if bad in required:
                    errors.append(f"{name}: {target} forbids secret '{bad}' (present)")
            # The repo's mechanism label, oidc or static-secret, must match the target mechanism's kind.
            # An OIDC mechanism may still require a non-secret stored value, NUGET_USERNAME for a NuGet login being one, so an empty requires list is not the signal.
            # The match is on the explicit kind instead.
            kind = spec_mech.get("kind")
            if kind and mech != kind:
                errors.append(f"{name}: {target} labeled '{mech}' but its mechanism is '{kind}'")

    # Every files.json appliesTo selector must resolve to a known token, and no project type may collide with a reserved selector, since a flat token set is only unambiguous while the namespaces stay disjoint.
    # An unknown token fails open, never matching, so a required file or section would silently apply nowhere.
    reserved = set(WORKFLOW_MODELS) | set(RELEASE_TRIGGERS) | set(CONSUMER_MODELS)
    clash = known_types & reserved
    if clash:
        errors.append(f"files.json: project type(s) collide with a reserved scope selector: {', '.join(sorted(clash))}")
    universe = known_types | reserved

    def check_selector(where, applies_to):
        if isinstance(applies_to, list) and not applies_to:
            errors.append(f"files.json: {where} appliesTo is an empty list (use \"*\" for all repos, or list selectors) - it would apply nowhere")
            return
        tokens = [] if applies_to == "*" else (applies_to if isinstance(applies_to, list) else [applies_to])
        for tok in tokens:
            # CI runs no JSON-schema validation, so guard the type here rather than crash on an unhashable token, a nested object being one, reaching the set-membership test below.
            if not isinstance(tok, str):
                errors.append(f"files.json: {where} appliesTo has a non-string token {tok!r}")
            elif tok not in universe:
                errors.append(f"files.json: {where} appliesTo '{tok}' is not a known selector")

    # CI runs no JSON-schema validation, so shape-check files.json here rather than crash on a malformed entry.
    # The shapes that reach this are a non-object baseline item, a non-array sections, and a section that is neither string nor object.
    files = load("spec/files.json")
    baseline = files.get("baseline", [])
    if not isinstance(baseline, list):
        errors.append("files.json: 'baseline' must be an array")
        baseline = []
    for item in baseline:
        if not isinstance(item, dict):
            errors.append(f"files.json: baseline entry {item!r} is not an object")
            continue
        path = item.get("path")
        if not isinstance(path, str):
            errors.append(f"files.json: baseline entry has a missing or non-string path: {item!r}")
            continue
        check_selector(path, item.get("appliesTo", "*"))

        # The fidelity field governs how faithfully the unit is checked, per spec/fidelity-model.md.
        # CI runs no schema validation, so shape-check the fidelity fields here rather than let a malformed contract or an outside-root reference slip through and crash a later check.
        fid = item.get("fidelity", "presence")
        if fid not in FIDELITIES:
            errors.append(f"files.json: {path} fidelity '{fid}' invalid (expected one of {', '.join(FIDELITIES)})")
        has_contract = "contract" in item
        if has_contract and fid != "interface":
            errors.append(f"files.json: {path} has a contract but fidelity is '{fid}' (contract is only for fidelity 'interface')")
        if fid == "interface" and not has_contract:
            errors.append(f"files.json: {path} fidelity 'interface' requires a contract")
        if has_contract:
            contract = item["contract"]
            if not isinstance(contract, dict):
                errors.append(f"files.json: {path} contract must be an object")
            else:
                unknown = set(contract) - CONTRACT_KEYS
                if unknown:
                    errors.append(f"files.json: {path} contract has unknown key(s): {', '.join(sorted(unknown))}")
                # The engine trusts these value types (CI runs no schema validation), so verify them here.
                for k in ("requiredJobKeys", "verbatimJobs"):
                    if k in contract and not is_str_list(contract[k]):
                        errors.append(f"files.json: {path} contract.{k} must be an array of strings")
                for k in ("requiredCheckName", "artifactNameToken"):
                    if k in contract and not isinstance(contract[k], str):
                        errors.append(f"files.json: {path} contract.{k} must be a string")
                for k in ("requireTokensInJob", "forbidTokensInJob"):
                    v = contract.get(k)
                    if k in contract and not (isinstance(v, dict) and all(isinstance(j, str) and is_str_list(t) for j, t in v.items())):
                        errors.append(f"files.json: {path} contract.{k} must be an object of job name to array of strings")
        ref = item.get("reference")
        if ref is not None and not isinstance(ref, str):
            errors.append(f"files.json: {path} reference must be a string")
            ref = None
        elif isinstance(ref, str) and (ref.startswith("/") or ".." in pathlib.PurePosixPath(ref).parts):
            errors.append(f"files.json: {path} reference '{ref}' must be a repo-relative path (no leading / or ..)")
        if fid == "verbatim":
            src = ref if isinstance(ref, str) else path
            if isinstance(src, str) and not (ROOT / src).exists():
                errors.append(f"files.json: {path} fidelity 'verbatim' but its canonical source {src} is missing")

        sections = item.get("sections", [])
        if not isinstance(sections, list):
            errors.append(f"files.json: {path} sections must be an array")
            continue
        for elt in sections:
            if isinstance(elt, dict):
                if not isinstance(elt.get("name"), str) or not elt.get("name"):
                    errors.append(f"files.json: {path} section object missing a non-empty string 'name': {elt!r}")
                check_selector(f"{path} section '{elt.get('name', '?')}'", elt.get("appliesTo", "*"))
                # A section may carry its own fidelity, defaulting to intent, or verbatim for a universal rule block checked byte-for-byte.
                # Verbatim is meaningful only on a Markdown file, where the heading delimits the region.
                # The hub's own file is the canonical, so no reference is needed.
                sfid = elt.get("fidelity", "intent")
                if sfid not in ("intent", "verbatim"):
                    errors.append(f"files.json: {path} section '{elt.get('name', '?')}' fidelity '{sfid}' invalid (expected intent or verbatim)")
                elif sfid == "verbatim" and not path.endswith(".md"):
                    errors.append(f"files.json: {path} section '{elt.get('name', '?')}' is verbatim but {path} is not Markdown (heading regions apply to .md only)")
            elif not isinstance(elt, str):
                errors.append(f"files.json: {path} section entry {elt!r} must be a string or object")

        # Every declared section must resolve to a real `## <heading>` in the hub's own copy of the file, per spec/section-model.md "Enforcement".
        # Without this, a renamed or mistyped section name declares a region that does not exist.
        # The downstream verbatim byte-match in audit.py then has nothing to compare, and the section silently stops being checked anywhere, which is the quiet-narrowing failure Verification Discipline forbids.
        # This is Markdown only, and only where the hub ships the file.
        if path.endswith(".md") and (ROOT / path).exists():
            hub_text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
            headings = {m.group(1).strip() for m in re.finditer(r"^## (.+?)\s*$", hub_text, re.M)}
            for elt in sections:
                name = elt.get("name") if isinstance(elt, dict) else elt
                if isinstance(name, str) and name and name not in headings:
                    errors.append(f"files.json: {path} declares section '{name}' but no '## {name}' heading exists in {path}")

    # Validate the divergence ledger in spec/divergences.json when present, so a mistyped repo name or disposition fails CI rather than silently dropping a burn-down row.
    dispositions = ("re-vendor", "track", "accepted", "upstream-candidate", "investigate", "retire")
    if (ROOT / "spec/divergences.json").exists():
        div = load("spec/divergences.json")
        repo_names = {r.get("name") for r in repos["repos"] if isinstance(r, dict)}
        manifest_paths = {i.get("path") for i in baseline if isinstance(i, dict)}
        # A verbatim section is an addressable unit too, labeled "path > section" to match fidelity_honesty's SECTION_SEP, so a section-scoped divergence can carry its own disposition.
        # Only well-formed section entries produce a label, since a malformed one is already reported by the files.json checks above.
        for i in baseline:
            if not isinstance(i, dict) or not isinstance(i.get("path"), str) or not isinstance(i.get("sections"), list):
                continue
            for elt in i["sections"]:
                if isinstance(elt, dict) and elt.get("fidelity") == "verbatim" and isinstance(elt.get("name"), str) and elt["name"]:
                    manifest_paths.add(f"{i['path']} > {elt['name']}")
        # Guard the root type: a non-object root (a list from a bad edit) would crash the .get() calls below.
        if not isinstance(div, dict):
            errors.append("divergences.json: root must be an object")
            div = {}
        div_dispositions = div.get("dispositions", [])
        if not isinstance(div_dispositions, list):
            errors.append("divergences.json: 'dispositions' must be an array")
            div_dispositions = []
        div_gaps = div.get("gaps", [])
        if not isinstance(div_gaps, list):
            errors.append("divergences.json: 'gaps' must be an array")
            div_gaps = []
        for d in div_dispositions:
            if not isinstance(d, dict):
                errors.append(f"divergences.json: disposition {d!r} is not an object")
                continue
            p = d.get("path")
            # The isinstance guard comes first, since a non-string path is unhashable and would crash the membership test.
            if not isinstance(p, str):
                errors.append(f"divergences.json: disposition path {p!r} must be a string")
            elif p not in manifest_paths:
                errors.append(f"divergences.json: disposition path '{p}' is not a manifest unit")
            if d.get("disposition") not in dispositions:
                errors.append(f"divergences.json: '{p}' disposition '{d.get('disposition')}' invalid (expected one of {', '.join(dispositions)})")
            if not is_str_list(d.get("repos")) or not d.get("repos"):
                errors.append(f"divergences.json: '{p}' repos must be a non-empty array of strings")
            else:
                for rn in d["repos"]:
                    if rn not in repo_names:
                        errors.append(f"divergences.json: '{p}' repo '{rn}' not in the registry")
            if not isinstance(d.get("reason"), str) or not d.get("reason"):
                errors.append(f"divergences.json: '{p}' reason must be a non-empty string")
            if not (d.get("tracking") is None or isinstance(d.get("tracking"), str)):
                errors.append(f"divergences.json: '{p}' tracking must be a string or null")
        for g in div_gaps:
            if not isinstance(g, dict):
                errors.append(f"divergences.json: gap {g!r} is not an object")
                continue
            gp = g.get("path")
            # The isinstance guard comes first, since a non-string path is unhashable and would crash the membership test.
            if not isinstance(gp, str):
                errors.append(f"divergences.json: gap path {gp!r} must be a string")
            elif gp in manifest_paths:
                errors.append(f"divergences.json: gap '{gp}' is already a manifest unit (not a gap)")
            if g.get("disposition") not in dispositions:
                errors.append(f"divergences.json: gap '{gp}' disposition '{g.get('disposition')}' invalid (expected one of {', '.join(dispositions)})")
            if not isinstance(g.get("reason"), str) or not g.get("reason"):
                errors.append(f"divergences.json: gap '{gp}' reason must be a non-empty string")
            if not (g.get("tracking") is None or isinstance(g.get("tracking"), str)):
                errors.append(f"divergences.json: gap '{gp}' tracking must be a string or null")

    if errors:
        print("Spec validation FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    cataloged = sum(1 for r in repos["repos"] if isinstance(r, dict) and r.get("status") == "cataloged")
    backlog = sum(1 for r in repos["repos"] if isinstance(r, dict) and r.get("status") == "backlog")
    print(f"Spec validation OK: {cataloged} cataloged, {backlog} backlog repos classify cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

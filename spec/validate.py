#!/usr/bin/env python3
"""Validate the registry and spec cross-references (stdlib only).

Checks that every cataloged repo classifies against the spec: its types resolve,
its publish mechanisms are recognized, and its secrets are consistent with
spec/secrets.json. Exits non-zero on any failure. This is the classification
dry-run the CI lint job runs; it needs no third-party packages.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


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

    # CI runs no JSON-schema validation, so shape-check secrets.json here to fail with a clear message
    # rather than crash the cross-reference loops below.
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

    # defaults.workflowModel feeds configure.sh's fallback, so an invalid value here breaks the apply while every
    # per-repo entry still validates - check it once.
    default_model = repos.get("defaults", {}).get("workflowModel")
    if default_model is not None and default_model not in ("release", "operational"):
        errors.append(f"defaults.workflowModel '{default_model}' invalid (expected release or operational)")

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

        model = repo.get("workflowModel")
        if model is not None and model not in ("release", "operational"):
            errors.append(f"{name}: workflowModel '{model}' invalid (expected release or operational)")

        eol = repo.get("lineEndings")
        if eol is not None and eol not in ("lf", "crlf"):
            errors.append(f"{name}: lineEndings '{eol}' invalid (expected lf or crlf)")
        # An operational repo's endings follow the consuming app's platform, so they must be declared; a release
        # repo omits the field and uses the fleet CRLF default.
        if model == "operational" and eol is None:
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
            # docker/static-secret must carry its required secrets
            for req in spec_mech.get("requires", []):
                if req not in required:
                    errors.append(f"{name}: {target} requires secret '{req}' (missing)")
            # oidc mechanisms must not carry a forbidden static key
            for bad in spec_mech.get("forbids", []):
                if bad in required:
                    errors.append(f"{name}: {target} forbids secret '{bad}' (present)")
            # mechanism label must match the target's expected mechanism family
            # The repo's mechanism label (oidc / static-secret) must match the target mechanism's kind.
            # An OIDC mechanism may still require a non-secret stored value (e.g. NUGET_USERNAME for
            # NuGet/login), so requires-emptiness is not the signal - match on the explicit kind.
            kind = spec_mech.get("kind")
            if kind and mech != kind:
                errors.append(f"{name}: {target} labeled '{mech}' but its mechanism is '{kind}'")

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

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
        ("types", types, "project-types.json"),
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

    for repo in repos["repos"]:
        name = repo["name"]
        if repo["status"] == "backlog":
            if not repo.get("classificationPending"):
                errors.append(f"{name}: backlog repo without classificationPending")
            continue

        for t in repo.get("types", []):
            if t not in known_types:
                errors.append(f"{name}: type '{t}' not defined in project-types.json")

        required = set(repo.get("requiredSecrets", []))
        for pub in repo.get("publish", []):
            target, mech = pub["target"], pub["mechanism"]
            if target not in target_mech:
                errors.append(f"{name}: publish target '{target}' unknown")
                continue
            mech_key = target_mech[target]
            if mech_key is None:
                continue
            spec_mech = mechanisms[mech_key]
            # docker/static-secret must carry its required secrets
            for req in spec_mech["requires"]:
                if req not in required:
                    errors.append(f"{name}: {target} requires secret '{req}' (missing)")
            # oidc mechanisms must not carry a forbidden static key
            for bad in spec_mech["forbids"]:
                if bad in required:
                    errors.append(f"{name}: {target} forbids secret '{bad}' (present)")
            # mechanism label must match the target's expected mechanism family
            if mech == "static-secret" and not spec_mech["requires"]:
                errors.append(f"{name}: {target} marked static-secret but mechanism needs no secret")
            if mech == "oidc" and spec_mech["requires"]:
                errors.append(f"{name}: {target} marked oidc but mechanism requires stored secrets")

    if errors:
        print("Spec validation FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    cataloged = sum(1 for r in repos["repos"] if r["status"] == "cataloged")
    backlog = sum(1 for r in repos["repos"] if r["status"] == "backlog")
    print(f"Spec validation OK: {cataloged} cataloged, {backlog} backlog repos classify cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

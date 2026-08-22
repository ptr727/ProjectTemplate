#!/usr/bin/env python3
"""Validate the registry and spec cross-references (stdlib only).

Checks that every cataloged repo classifies against the spec: its types resolve,
its publish mechanisms are recognized, and its secrets are consistent with
spec/secrets.json. Exits non-zero on any failure. This is the classification
dry-run the CI lint job runs; it needs no third-party packages.
"""

import fnmatch
import json
import pathlib
import posixpath
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
CONTRACT_KEYS = {
    "requiredJobKeys",
    "requiredCheckName",
    "artifactNameToken",
    "requireTokensInJob",
    "forbidTokensInJob",
    "verbatimJobs",
}

MARKDOWN_INLINE_LINK = re.compile(r"\]\((?P<target>[^)\s]+)")
MARKDOWN_REFERENCE_LINK = re.compile(r"^\[[^]]+\]:\s*(?P<target>\S+)", re.MULTILINE)
TEMPLATE_REPOSITORY_URL = "https://github.com/ptr727/ProjectTemplate"


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def is_str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def markdown_targets(text):
    """Yield link targets outside fenced blocks."""
    visible = []
    in_fence = False
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if not in_fence:
            visible.append(line)
    body = re.sub(r"`+[^`]*`+", "", "\n".join(visible))
    for pattern in (MARKDOWN_INLINE_LINK, MARKDOWN_REFERENCE_LINK):
        yield from (match.group("target") for match in pattern.finditer(body))


def markdown_section(text, name):
    """Return one level-two Markdown section, excluding its heading."""
    match = re.search(rf"^## {re.escape(name)}\s*$", text, re.MULTILINE)
    if not match:
        return ""
    following = re.search(r"^## ", text[match.end() :], re.MULTILINE)
    end = len(text) if following is None else match.end() + following.start()
    return text[match.end() : end]


def carried_link_errors(root, baseline):
    """Reject links that stop being truthful when their Markdown source is carried."""
    universal = {
        item["path"]
        for item in baseline
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and item.get("appliesTo", "*") == "*"
    }
    errors = []
    for item in baseline:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        source = item["path"]
        path = root / source
        if not source.endswith(".md") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        sections = item.get("sections", [])
        regions = []
        if item.get("fidelity") == "intent" and (item.get("whole") is True or not sections):
            regions.append(("whole file", text))
        for section in sections if isinstance(sections, list) else []:
            if isinstance(section, dict) and section.get("fidelity") == "verbatim":
                name = section.get("name")
                if isinstance(name, str):
                    regions.append((f"section '{name}'", markdown_section(text, name)))
        for region, body in regions:
            for target in markdown_targets(body):
                repository_target = target.split("#", 1)[0]
                is_template_link = (
                    repository_target == TEMPLATE_REPOSITORY_URL
                    or repository_target.startswith(f"{TEMPLATE_REPOSITORY_URL}/")
                )
                if is_template_link and source != "AUDIT.md":
                    errors.append(
                        f"files.json: {source} {region} links to the template repository "
                        f"at '{target}'"
                    )
                    continue
                relative_target = target.split("#", 1)[0]
                if (
                    not relative_target
                    or "://" in relative_target
                    or relative_target.startswith(("mailto:", "#"))
                ):
                    continue
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(source), relative_target)
                )
                if resolved not in universal:
                    errors.append(
                        f"files.json: {source} {region} links to relative target "
                        f"'{relative_target}', which is not universally carried"
                    )
    return errors


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
        for err in errors:
            print(f"  - {err}")
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
        for key, want in (
            ("sections", list),
            ("shieldClasses", list),
            ("linkGroups", list),
            ("linkNaming", list),
            ("canonicalLinks", list),
            ("distribution", dict),
        ):
            value = readme_model.get(key)
            if not isinstance(value, want) or not value:
                errors.append(
                    f"readme-sections.json: '{key}' must be a non-empty {'array' if want is list else 'object'}, and spec/audit.py indexes it directly"
                )
        # The distribution prefixes are what tell a repo's own URLs from a third party's.
        # Their absence fails open rather than loud: link_kind would classify every own URL as external, canonical naming would quietly stop being enforced, and the audit would still report green.
        prefixes = (
            readme_model.get("distribution", {}).get("urlPrefixes")
            if isinstance(readme_model.get("distribution"), dict)
            else None
        )
        if not is_str_list(prefixes) or not prefixes:
            errors.append(
                "readme-sections.json: 'distribution.urlPrefixes' must be a non-empty array of strings, or the link audit stops distinguishing this repo's URLs from a third party's and silently passes"
            )
        else:
            # Every prefix, not merely one of them.
            # A broad entry added beside a valid one would pass an any() guard while making link_kind read a third party's URL as this repo's own, which is the failure the guard exists to stop.
            loose = [p for p in prefixes if "{slug}" not in p and "{owner}" not in p]
            if loose:
                errors.append(
                    f"readme-sections.json: 'distribution.urlPrefixes' entry {loose[0]!r} carries neither {{slug}} nor {{owner}}, so it is not repo-scoped and would match another owner's URLs"
                )
        # A canonicalLinks entry naming a destination it cannot match is a name nothing enforces, and audit.py raises on it mid-run rather than reporting.
        for c in (
            readme_model.get("canonicalLinks", [])
            if isinstance(readme_model.get("canonicalLinks"), list)
            else []
        ):
            if isinstance(c, dict) and "repoPath" not in c and "match" not in c:
                errors.append(
                    f"readme-sections.json: canonicalLinks entry '{c.get('name')}' carries neither 'repoPath' nor 'match', so it can match no URL"
                )

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
            if not isinstance(t, dict) or not all(
                isinstance(t.get(f), str) and t.get(f) for f in ("name", "link", "description")
            ):
                errors.append(
                    f"third-party-tools.json: entry {name or t!r} needs a non-empty name, link and description"
                )
                continue
            if name.lower() in seen:
                errors.append(
                    f"third-party-tools.json: duplicate tool name '{name}' - the audit keys on it, so the second entry would shadow the first"
                )
            seen.add(name.lower())
            desc = t["description"]
            if not (desc[0].isupper() and desc.endswith(".")):
                errors.append(
                    f"third-party-tools.json: '{name}' description {desc!r} is not a sentence - open with a capital and close with a full stop"
                )
        names = [t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]
        if names != sorted(names, key=str.lower):
            errors.append(
                "third-party-tools.json: 'tools' is not sorted by name, which is how a reader finds an entry to copy"
            )
    # Shape-checked here rather than left to the gate, because a malformed entry is a silently skipped tool.
    # A floor nobody can compare against reports nothing, which reads exactly like a host that passed.
    host_tools = load("spec/host-tools.json")
    if not isinstance(host_tools, dict):
        errors.append("host-tools.json: top level is not an object")
    elif not isinstance(host_tools.get("tools"), list) or not host_tools["tools"]:
        errors.append("host-tools.json: 'tools' must be a non-empty array")
    else:
        ht_seen = set()
        for t in host_tools["tools"]:
            name = t.get("name") if isinstance(t, dict) else None
            if not isinstance(t, dict) or not isinstance(name, str) or not name:
                errors.append(f"host-tools.json: entry {t!r} needs a non-empty name")
                continue
            # Case-insensitive, matching the gate, which folds case so a repository writing 'GH' overrides 'gh' rather than adding a second entry.
            if name.lower() in ht_seen:
                errors.append(
                    f"host-tools.json: duplicate tool name '{name}' - the gate keys on it without regard to case, so the second entry would shadow the first"
                )
            ht_seen.add(name.lower())
            if not isinstance(t.get("why"), str) or not t.get("why"):
                errors.append(
                    f"host-tools.json: '{name}' needs a non-empty why, which is what keeps a floor from becoming folklore"
                )
            # Type-checked rather than read for truthiness, since the string "false" is true and would fail every host on a tool nobody requires.
            # CI runs no JSON-schema validation, so this file is the only thing that reads the declaration before the gate trusts it.
            if "required" in t and not isinstance(t["required"], bool):
                errors.append(
                    f"host-tools.json: '{name}' required must be true or false, not {t['required']!r}"
                )
            # Compiled rather than only type-checked, since scripts/host_gate.py names this file as what covers the hub declaration.
            # An uncompilable pattern would otherwise ship and surface at gate runtime, which is the reader that cannot fix it.
            if not isinstance(t.get("pattern"), str) or not t.get("pattern"):
                errors.append(
                    f"host-tools.json: '{name}' needs a non-empty pattern to read a version with"
                )
            else:
                try:
                    re.compile(t["pattern"])
                except re.error as e:
                    errors.append(f"host-tools.json: '{name}' pattern does not compile ({e})")
            probes = t.get("probes")
            # The emptiness of each argument is read as well as its type, so this says what it claims and agrees with the gate's own check.
            # An empty argument passes a type test and produces a probe that cannot execute.
            if (
                not isinstance(probes, list)
                or not probes
                or not all(
                    isinstance(p, list) and p and all(isinstance(a, str) and a for a in p)
                    for p in probes
                )
            ):
                errors.append(
                    f"host-tools.json: '{name}' needs 'probes' as a non-empty array of non-empty string arrays"
                )
            # Presence is read as presence, since a sentinel default cannot tell a missing key from one holding that same value.
            # A declared "minimum": false would otherwise be reported as undeclared, sending the reader to add a field that is already there.
            if "minimum" not in t:
                errors.append(
                    f"host-tools.json: '{name}' must declare 'minimum', using null where no floor has been measured"
                )
                floor = None
            else:
                floor = t["minimum"]
            if floor is not None:
                if not isinstance(floor, str) or not re.fullmatch(r"\d+(\.\d+)*", floor):
                    errors.append(
                        f"host-tools.json: '{name}' minimum {floor!r} must be dot-separated integers or null"
                    )
                elif not isinstance(t.get("source"), dict) or not t["source"]:
                    errors.append(
                        f"host-tools.json: '{name}' declares a floor and no 'source', so a host below it is told to upgrade and not where from"
                    )
                else:
                    # The keys are read by platform, so a misspelled one drops the remedy on that platform while the object stays non-empty.
                    # Requiring the object and not its contents is the shape of guard this repo keeps finding: present, and asserting nothing.
                    platforms = {"linux", "macos", "windows"}
                    stray = sorted(set(t["source"]) - platforms)
                    if stray:
                        errors.append(
                            f"host-tools.json: '{name}' source names {', '.join(stray)}, which no platform reads - use {', '.join(sorted(platforms))}"
                        )
                    for plat, where in t["source"].items():
                        if not isinstance(where, str) or not where:
                            errors.append(
                                f"host-tools.json: '{name}' source.{plat} must be a non-empty string"
                            )
                    # The remedy is held to the same shape as the source it sits beside, and required on the same trigger.
                    # A floor whose failure prints no runnable command leaves the operator to rediscover the installer, which is the gap the field closes.
                    if not isinstance(t.get("remedy"), dict) or not t["remedy"]:
                        errors.append(
                            f"host-tools.json: '{name}' declares a floor and no 'remedy', so a below-floor failure names no command that fixes it"
                        )
                    else:
                        stray = sorted(set(t["remedy"]) - platforms)
                        if stray:
                            errors.append(
                                f"host-tools.json: '{name}' remedy names {', '.join(stray)}, which no platform reads - use {', '.join(sorted(platforms))}"
                            )
                        for plat, command in t["remedy"].items():
                            if not isinstance(command, str) or not command:
                                errors.append(
                                    f"host-tools.json: '{name}' remedy.{plat} must be a non-empty string"
                                )
        ht_names = [
            t["name"]
            for t in host_tools["tools"]
            if isinstance(t, dict) and isinstance(t.get("name"), str)
        ]
        if ht_names != sorted(ht_names, key=str.lower):
            errors.append(
                "host-tools.json: 'tools' is not sorted by name, which is how a reader finds an entry"
            )
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
                errors.append(
                    f"secrets.json: targetMechanisms['{t}'] must be a mechanism name or null"
                )
    feature_mech = secrets.get("featureMechanisms", {})
    if not isinstance(feature_mech, dict):
        errors.append("secrets.json: 'featureMechanisms' is not an object")
    else:
        for f, v in feature_mech.items():
            if not (v is None or isinstance(v, str)):
                errors.append(
                    f"secrets.json: featureMechanisms['{f}'] must be a mechanism name or null"
                )
    type_mech = secrets.get("typeMechanisms", {})
    if not isinstance(type_mech, dict):
        errors.append("secrets.json: 'typeMechanisms' is not an object")
    else:
        for t, v in type_mech.items():
            if not (v is None or isinstance(v, str)):
                errors.append(
                    f"secrets.json: typeMechanisms['{t}'] must be a mechanism name or null"
                )
    if errors:
        print("Spec validation FAILED:")
        for err in errors:
            print(f"  - {err}")
        return 1

    # The defaults for workflowModel and releaseTrigger feed configure.sh's fallback and selector resolution.
    # An invalid value there breaks the apply or scopes wrong while every per-repo entry still validates, so check them once.
    reg_defaults = repos.get("defaults", {})
    default_model = reg_defaults.get("workflowModel")
    if default_model is not None and default_model not in WORKFLOW_MODELS:
        errors.append(
            f"defaults.workflowModel '{default_model}' invalid (expected {' or '.join(WORKFLOW_MODELS)})"
        )
    default_trigger = reg_defaults.get("releaseTrigger")
    if default_trigger is not None and default_trigger not in RELEASE_TRIGGERS:
        errors.append(
            f"defaults.releaseTrigger '{default_trigger}' invalid (expected one of {', '.join(RELEASE_TRIGGERS)})"
        )

    for i, repo in enumerate(repos["repos"]):
        if not isinstance(repo, dict):
            errors.append(f"repo #{i} is not an object")
            continue
        name = repo.get("name", f"#{i}")
        # No status, cataloged included, ever validated name or url beyond this fallback.
        # A missing or blank one passed cleanly here and only broke a downstream consumer later.
        # One such consumer, spec/audit.py's membership_findings(), indexes the registry by repo["name"] and needs every entry to actually have one.
        if not isinstance(repo.get("name"), str) or not repo["name"].strip():
            errors.append(f"repo #{i}: missing or empty 'name'")
            continue
        if not isinstance(repo.get("url"), str) or not repo["url"].strip():
            errors.append(f"{name}: missing or empty 'url'")
            continue
        status = repo.get("status")
        if status is None:
            errors.append(f"{name}: missing 'status'")
            continue
        if status == "backlog":
            if not repo.get("classificationPending"):
                errors.append(f"{name}: backlog repo without classificationPending")
            continue
        if status == "archived":
            # GitHub's own archived flag is the fact.
            # The entry only needs to exist, so spec/audit.py's fleet membership check has something to match it against.
            continue
        if status == "excluded":
            reason = repo.get("exclusionReason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{name}: excluded repo without a non-empty exclusionReason")
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
                errors.append(
                    f"{name}: profile declared for '{tname}', not one of the repo's types"
                )
            elif tname in known_types and prof not in allowed:
                errors.append(
                    f"{name}: type '{tname}' profile '{prof}' not in its allowed profiles {allowed or '[]'}"
                )

        model = repo.get("workflowModel")
        if model is not None and model not in WORKFLOW_MODELS:
            errors.append(
                f"{name}: workflowModel '{model}' invalid (expected {' or '.join(WORKFLOW_MODELS)})"
            )

        # The releaseTrigger field is a scope selector, per spec/scope-model.md, so an invalid value would silently fail to match any releaseTrigger-scoped section rather than error.
        trigger = repo.get("releaseTrigger")
        if trigger is not None and trigger not in RELEASE_TRIGGERS:
            errors.append(
                f"{name}: releaseTrigger '{trigger}' invalid (expected one of {', '.join(RELEASE_TRIGGERS)})"
            )

        # The consumerModel field is a scope selector, per spec/scope-model.md, so a cataloged repo must declare it.
        # Otherwise a push-scoped or pull-scoped section would fail open on that repo, never matching.
        cm = repo.get("consumerModel")
        if cm not in CONSUMER_MODELS:
            errors.append(
                f"{name}: consumerModel '{cm}' invalid or missing (expected {' or '.join(CONSUMER_MODELS)})"
            )

        eol = repo.get("lineEndings")
        if eol is not None and eol not in ("lf", "crlf"):
            errors.append(f"{name}: lineEndings '{eol}' invalid (expected lf or crlf)")
        # An operational repo's endings follow the consuming app's platform, so they must be declared, where a release repo omits the field and takes the fleet LF default.
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
        errors.append(
            f"files.json: project type(s) collide with a reserved scope selector: {', '.join(sorted(clash))}"
        )
    universe = known_types | reserved

    def check_selector(where, applies_to):
        if isinstance(applies_to, list) and not applies_to:
            errors.append(
                f'files.json: {where} appliesTo is an empty list (use "*" for all repos, or list selectors) - it would apply nowhere'
            )
            return
        tokens = (
            []
            if applies_to == "*"
            else (applies_to if isinstance(applies_to, list) else [applies_to])
        )
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

    if "trees" not in files:
        errors.append("files.json: missing required 'trees' array")
    trees = files.get("trees", [])
    if not isinstance(trees, list):
        errors.append("files.json: 'trees' must be an array")
        trees = []
    validated_trees = []
    for tree in trees:
        if not isinstance(tree, dict):
            errors.append(f"files.json: tree declaration {tree!r} is not an object")
            continue
        source = tree.get("source")
        target = tree.get("target")
        if not isinstance(source, str) or not source:
            errors.append(f"files.json: tree declaration has an invalid source: {tree!r}")
            continue
        if not isinstance(target, str) or not target:
            errors.append(f"files.json: tree declaration has an invalid target: {tree!r}")
            continue
        for field, value in (("source", source), ("target", target)):
            parts = pathlib.PurePosixPath(value).parts
            if value == "." or value.startswith("/") or ".." in parts:
                errors.append(
                    f"files.json: tree {source} {field} '{value}' must be below the repository root (no leading /, . or ..)"
                )
        if tree.get("fidelity") != "verbatim-tree":
            errors.append(f"files.json: tree {source} fidelity must be 'verbatim-tree'")
        if "appliesTo" not in tree:
            errors.append(f"files.json: tree {source} is missing required appliesTo")
        else:
            check_selector(f"tree {source}", tree["appliesTo"])
        include = tree.get("include")
        if not is_str_list(include) or not include:
            errors.append(f"files.json: tree {source} include must be a non-empty array of strings")
        if not isinstance(tree.get("prune"), bool):
            errors.append(f"files.json: tree {source} prune must be a boolean")
        if "allowHubTarget" in tree and not isinstance(tree["allowHubTarget"], bool):
            errors.append(f"files.json: tree {source} allowHubTarget must be a boolean")
        if not (ROOT / source).is_dir():
            errors.append(
                f"files.json: tree canonical source {source} is missing or not a directory"
            )
        validated_trees.append(tree)

    for index, left in enumerate(validated_trees):
        left_target = pathlib.PurePosixPath(left["target"])
        for right in validated_trees[index + 1 :]:
            right_target = pathlib.PurePosixPath(right["target"])
            overlaps = (
                left_target == right_target
                or left_target in right_target.parents
                or right_target in left_target.parents
            )
            if overlaps:
                errors.append(
                    f"files.json: tree targets {left_target} and {right_target} have overlapping ownership"
                )

    copilot_path = ROOT / ".github/copilot-instructions.md"
    try:
        copilot_instructions = copilot_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"files.json: cannot read {copilot_path.relative_to(ROOT)}: {exc}")
        copilot_instructions = ""
    named_skills = set(
        re.findall(r"\.github/skills/[A-Za-z0-9_-]+/SKILL\.md", copilot_instructions)
    )
    for path in sorted(named_skills):
        if not (ROOT / path).is_file():
            errors.append(f"files.json: Copilot instructions reference missing skill path {path}")
        skill_path = pathlib.PurePosixPath(path)
        carried = False
        for tree in validated_trees:
            target_root = pathlib.PurePosixPath(tree["target"])
            include = tree.get("include")
            if target_root not in skill_path.parents or not is_str_list(include):
                continue
            relative = skill_path.relative_to(target_root).as_posix()
            carried = any(
                pattern == "**/*"
                or fnmatch.fnmatchcase(relative, pattern)
                or pathlib.PurePosixPath(relative).match(pattern)
                for pattern in include
            )
            if carried:
                break
        if not carried:
            errors.append(
                f"files.json: Copilot instructions reference skill path {path} outside every carried tree include"
            )
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
            errors.append(
                f"files.json: {path} fidelity '{fid}' invalid (expected one of {', '.join(FIDELITIES)})"
            )
        has_contract = "contract" in item
        if has_contract and fid != "interface":
            errors.append(
                f"files.json: {path} has a contract but fidelity is '{fid}' (contract is only for fidelity 'interface')"
            )
        if fid == "interface" and not has_contract:
            errors.append(f"files.json: {path} fidelity 'interface' requires a contract")
        if has_contract:
            contract = item["contract"]
            if not isinstance(contract, dict):
                errors.append(f"files.json: {path} contract must be an object")
            else:
                unknown = set(contract) - CONTRACT_KEYS
                if unknown:
                    errors.append(
                        f"files.json: {path} contract has unknown key(s): {', '.join(sorted(unknown))}"
                    )
                # The engine trusts these value types (CI runs no schema validation), so verify them here.
                for k in ("requiredJobKeys", "verbatimJobs"):
                    if k in contract and not is_str_list(contract[k]):
                        errors.append(
                            f"files.json: {path} contract.{k} must be an array of strings"
                        )
                for k in ("requiredCheckName", "artifactNameToken"):
                    if k in contract and not isinstance(contract[k], str):
                        errors.append(f"files.json: {path} contract.{k} must be a string")
                for k in ("requireTokensInJob", "forbidTokensInJob"):
                    v = contract.get(k)
                    if k in contract and not (
                        isinstance(v, dict)
                        and all(isinstance(j, str) and is_str_list(t) for j, t in v.items())
                    ):
                        errors.append(
                            f"files.json: {path} contract.{k} must be an object of job name to array of strings"
                        )
        ref = item.get("reference")
        if ref is not None and not isinstance(ref, str):
            errors.append(f"files.json: {path} reference must be a string")
            ref = None
        elif isinstance(ref, str) and (
            ref.startswith("/") or ".." in pathlib.PurePosixPath(ref).parts
        ):
            errors.append(
                f"files.json: {path} reference '{ref}' must be a repo-relative path (no leading / or ..)"
            )
        if fid == "verbatim":
            src = ref if isinstance(ref, str) else path
            if isinstance(src, str) and not (ROOT / src).exists():
                errors.append(
                    f"files.json: {path} fidelity 'verbatim' but its canonical source {src} is missing"
                )

        sections = item.get("sections", [])
        if not isinstance(sections, list):
            errors.append(f"files.json: {path} sections must be an array")
            continue
        for elt in sections:
            if isinstance(elt, dict):
                if not isinstance(elt.get("name"), str) or not elt.get("name"):
                    errors.append(
                        f"files.json: {path} section object missing a non-empty string 'name': {elt!r}"
                    )
                check_selector(
                    f"{path} section '{elt.get('name', '?')}'", elt.get("appliesTo", "*")
                )
                # A section may carry its own fidelity, defaulting to intent, or verbatim for a universal rule block checked byte-for-byte.
                # Verbatim is meaningful only on a Markdown file, where the heading delimits the region.
                # The hub's own file is the canonical, so no reference is needed.
                sfid = elt.get("fidelity", "intent")
                if sfid not in ("intent", "verbatim"):
                    errors.append(
                        f"files.json: {path} section '{elt.get('name', '?')}' fidelity '{sfid}' invalid (expected intent or verbatim)"
                    )
                elif sfid == "verbatim" and not path.endswith(".md"):
                    errors.append(
                        f"files.json: {path} section '{elt.get('name', '?')}' is verbatim but {path} is not Markdown (heading regions apply to .md only)"
                    )
            elif not isinstance(elt, str):
                errors.append(
                    f"files.json: {path} section entry {elt!r} must be a string or object"
                )

        # Every declared section must resolve to a real `## <heading>` in the hub's own copy of the file, per spec/section-model.md "Enforcement".
        # Without this, a renamed or mistyped section name declares a region that does not exist.
        # The downstream verbatim byte-match in audit.py then has nothing to compare, and the section silently stops being checked anywhere, which is the quiet-narrowing failure Verification Discipline forbids.
        # This is Markdown only, and only where the hub ships the file.
        if path.endswith(".md") and (ROOT / path).exists():
            hub_text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
            headings = {
                m.group(1).strip() for m in re.finditer(r"^## (.+?)\s*$", hub_text, re.MULTILINE)
            }
            for elt in sections:
                name = elt.get("name") if isinstance(elt, dict) else elt
                if isinstance(name, str) and name and name not in headings:
                    errors.append(
                        f"files.json: {path} declares section '{name}' but no '## {name}' heading exists in {path}"
                    )

    errors.extend(carried_link_errors(ROOT, baseline))

    # Validate the divergence ledger in spec/divergences.json when present, so a mistyped repo name or disposition fails CI rather than silently dropping a burn-down row.
    dispositions = ("re-vendor", "track", "accepted", "upstream-candidate", "investigate", "retire")
    if (ROOT / "spec/divergences.json").exists():
        div = load("spec/divergences.json")
        repo_names = {r.get("name") for r in repos["repos"] if isinstance(r, dict)}
        manifest_paths = {i.get("path") for i in baseline if isinstance(i, dict)}
        # A verbatim section is an addressable unit too, labeled "path > section" to match fidelity_honesty's SECTION_SEP, so a section-scoped divergence can carry its own disposition.
        # Only well-formed section entries produce a label, since a malformed one is already reported by the files.json checks above.
        for i in baseline:
            if (
                not isinstance(i, dict)
                or not isinstance(i.get("path"), str)
                or not isinstance(i.get("sections"), list)
            ):
                continue
            for elt in i["sections"]:
                if (
                    isinstance(elt, dict)
                    and elt.get("fidelity") == "verbatim"
                    and isinstance(elt.get("name"), str)
                    and elt["name"]
                ):
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
                errors.append(
                    f"divergences.json: '{p}' disposition '{d.get('disposition')}' invalid (expected one of {', '.join(dispositions)})"
                )
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
                errors.append(
                    f"divergences.json: gap '{gp}' is already a manifest unit (not a gap)"
                )
            if g.get("disposition") not in dispositions:
                errors.append(
                    f"divergences.json: gap '{gp}' disposition '{g.get('disposition')}' invalid (expected one of {', '.join(dispositions)})"
                )
            if not isinstance(g.get("reason"), str) or not g.get("reason"):
                errors.append(f"divergences.json: gap '{gp}' reason must be a non-empty string")
            if not (g.get("tracking") is None or isinstance(g.get("tracking"), str)):
                errors.append(f"divergences.json: gap '{gp}' tracking must be a string or null")

    if errors:
        print("Spec validation FAILED:")
        for err in errors:
            print(f"  - {err}")
        return 1
    cataloged = sum(
        1 for r in repos["repos"] if isinstance(r, dict) and r.get("status") == "cataloged"
    )
    backlog = sum(1 for r in repos["repos"] if isinstance(r, dict) and r.get("status") == "backlog")
    archived = sum(
        1 for r in repos["repos"] if isinstance(r, dict) and r.get("status") == "archived"
    )
    excluded = sum(
        1 for r in repos["repos"] if isinstance(r, dict) and r.get("status") == "excluded"
    )
    print(
        f"Spec validation OK: {cataloged} cataloged repos classify cleanly. "
        f"{backlog} backlog repos await classification. "
        f"{archived} archived, {excluded} excluded repos carry a valid entry."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

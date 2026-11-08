"""Which secret names a registry repo owes, and the host file each one is read from.

The audit (spec/audit.py) and the provisioning scripts (repo-config/configure.sh through spec/resolve_secrets.py)
both need the same answer, so it is computed here once.
This module imports only the standard library.
That is because configure.sh runs it under whatever plain python3 it finds, where audit.py's yaml and markdown-it
imports are absent.
"""

from __future__ import annotations

from typing import Any

STORES = ("actions", "dependabot")

# A fleet secret has one value, shared by every repo that needs it.
# A repository secret has a value of its own for each repo.
SECRET_SCOPES = ("fleet", "repository")


class SecretNameError(Exception):
    """A secret declaration the caller must fail loud on rather than read around."""


def declared_types(entry: Any) -> list[str]:
    """The string elements of the registry entry's `types` list, or an empty list where `types` is not a list."""
    types = entry.get("types")
    return [t for t in types if isinstance(t, str)] if isinstance(types, list) else []


def ground_branch_of(
    entry: dict[str, Any], branch: str | None = None, defaults: dict[str, Any] | None = None
) -> Any:
    """The branch the audit and the provisioning read, the registry's `groundTruthBranch` unless overridden.

    A declared empty string is kept for spec/validate.py to refuse, rather than defaulted away.
    """
    if branch:
        return branch
    if "groundTruthBranch" in entry:
        return entry["groundTruthBranch"]
    defaults = defaults or {}
    if "groundTruthBranch" in defaults:
        return defaults["groundTruthBranch"]
    return "main"


def tree_blobs(tree: Any) -> dict[str, tuple[Any, Any]] | None:
    """Every blob path of a git trees API response mapped to its (SHA, mode), or None when it cannot be read in full.

    A response that is empty, holds no `tree` list, or is truncated is unreadable.
    A truncated tree would silently drop names, so both callers refuse it rather than use part of it.
    """
    if not tree or not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
        return None
    if tree.get("truncated"):
        return None
    return {
        n["path"]: (n.get("sha"), n.get("mode")) for n in tree["tree"] if n.get("type") == "blob"
    }


def in_test_directory(path: str) -> bool:
    """Whether any directory segment of path names a test directory, compared case-insensitively."""
    return any(segment.lower() in ("test", "tests") for segment in path.split("/")[:-1])


def is_python_test_module(path: str) -> bool:
    """Whether path is a module pytest discovers by name, wherever in the tree it sits."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_csharp_test_project(path: str) -> bool:
    """Whether path is a C# project either named or located as a test project."""
    if not path.endswith(".csproj"):
        return False
    name = path.rsplit("/", 1)[-1]
    segments = name[: -len(".csproj")].lower().split(".")
    return (
        "Test" in name
        or any(segment in ("test", "tests") for segment in segments)
        or in_test_directory(path)
    )


def coverage_candidate_types(types: list[str], repo_profiles: Any) -> list[str]:
    """The declared types whose profile leaves them owing coverage at all, before tests are looked for."""
    profiles = repo_profiles if isinstance(repo_profiles, dict) else {}
    return [t for t in types if t == "python" or profiles.get(t) != "lint-only"]


def coverage_claiming_types(
    types: list[str], type_mechanisms: dict[str, Any], tree: set[str] | None
) -> list[str]:
    """The declared types that owe Codecov coverage, meaning the CODECOV_TOKEN secret and codecov.yml.

    `tree` is the blob path set, or None when unread, and then every candidate keeps its claim.
    """
    detectors = {
        "csharp": lambda paths: any(is_csharp_test_project(p) for p in paths),
        "python": lambda paths: any(
            (p.endswith(".py") and in_test_directory(p)) or is_python_test_module(p) for p in paths
        ),
    }
    claiming = []
    for name in types:
        if type_mechanisms.get(name) != "codecov":
            continue
        detect = detectors.get(name)
        if tree is None or detect is None or detect(tree):
            claiming.append(name)
    return claiming


def resolve_secret_stores(
    secrets: dict[str, Any], entry: dict[str, Any], tree: set[str] | None
) -> tuple[dict[str, set[str]], set[str]]:
    """The secret names each store must hold for this repo, and the names that must be absent from every store.

    The mechanisms come from the declared publish targets and the declared types, never from workflow content.
    A repo's tree decides only whether a csharp or python repo owes the codecov mechanism, through its tests.
    """
    mechanisms = [
        secrets["targetMechanisms"].get(p.get("target")) for p in entry.get("publish", [])
    ]
    candidates = coverage_candidate_types(declared_types(entry), entry.get("profiles", {}))
    coverage_types = coverage_claiming_types(candidates, secrets.get("typeMechanisms", {}), tree)
    for name in declared_types(entry):
        mechanism = secrets.get("typeMechanisms", {}).get(name)
        if mechanism != "codecov" or name in coverage_types:
            mechanisms.append(mechanism)
    claimed = [secrets["mechanisms"][m] for m in mechanisms if m and m in secrets["mechanisms"]]
    required: dict[str, set[str]] = {store: set() for store in STORES}
    for store in secrets["baseline"].get("stores", []):
        required[store] |= set(secrets["baseline"].get("requires", []))
    for mech in claimed:
        for store in mech.get("stores", []):
            required[store] |= set(mech.get("requires", []))
    # The registry requiredSecrets are the domain-specific additions, per STANDUP.md, expected in the actions store.
    required["actions"] |= set(entry.get("requiredSecrets", []))
    forbidden = set(secrets["baseline"].get("forbids", []))
    for mech in claimed:
        forbidden |= set(mech.get("forbids", []))
    return required, forbidden


def secret_file_name(name: str, scope: str, repo: str) -> str:
    """The file under ~/.secrets/ holding the value of secret NAME, the one place these names are made.

    A fleet secret reads `github_<name>` and a repository secret reads `github_<repo>_<name>`.
    Both are lower case, and a repo name keeps its hyphens.
    """
    if scope == "fleet":
        return f"github_{name.lower()}"
    if scope == "repository":
        return f"github_{repo.lower()}_{name.lower()}"
    raise SecretNameError(f"secret {name} has the scope {scope!r}, not one of {SECRET_SCOPES}")


def wrong_scope_file_name(name: str, scope: str, repo: str) -> str:
    """The file a secret would have at the other scope, which must never exist beside the right one."""
    other = "repository" if scope == "fleet" else "fleet"
    return secret_file_name(name, other, repo)


def secret_scope(secrets: dict[str, Any], name: str) -> str:
    """The declared scope of NAME, raising for a name that has none rather than defaulting one."""
    scopes = secrets.get("secretScopes")
    if not isinstance(scopes, dict):
        raise SecretNameError("secrets.json has no secretScopes object")
    scope = scopes.get(name)
    if scope not in SECRET_SCOPES:
        raise SecretNameError(f"secret {name} has no scope of {' or '.join(SECRET_SCOPES)}")
    return str(scope)


def secret_plan(
    secrets: dict[str, Any], entry: dict[str, Any], tree: set[str] | None, repo: str
) -> dict[str, Any]:
    """One row per required secret name, the forbidden names, and the wrong-scope file names for this repo.

    Each row carries the name, its scope, the file it is read from, and the stores that need it.
    A stray is the file the name would have at the other scope.
    """
    required, forbidden = resolve_secret_stores(secrets, entry, tree)
    rows = []
    strays = set()
    for name in sorted(set().union(*required.values())):
        scope = secret_scope(secrets, name)
        rows.append(
            {
                "name": name,
                "scope": scope,
                "file": secret_file_name(name, scope, repo),
                "stores": [store for store in STORES if name in required[store]],
            }
        )
        strays.add(wrong_scope_file_name(name, scope, repo))
    strays -= {row["file"] for row in rows}
    return {"rows": rows, "forbidden": sorted(forbidden), "strays": sorted(strays)}

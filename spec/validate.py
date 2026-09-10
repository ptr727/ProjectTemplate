#!/usr/bin/env python3
"""Validate the registry and spec cross-references (stdlib only).

Checks that every cataloged repo classifies against the spec: its types resolve,
its publish mechanisms are recognized, and its secrets are consistent with
spec/secrets.json. Exits non-zero on any failure. This is the classification
dry-run the CI lint job runs; it needs no third-party packages.
"""

import collections
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
RELEASE_TRIGGERS = ("two-phase", "dispatch-only", "none")
CONSUMER_MODELS = ("push", "pull")
# The deployment-branch-policy forms a repo's `environments` entry may declare, kept in sync with the $defs in registry/repos.schema.json.
# This is not a scope selector: it names no audit scope and selects nothing in spec/files.json, so it sits outside the disjointness rule above.
# GitHub's "protected branches only" form is deliberately absent, since it counts classic branch protection and this fleet configures rulesets instead.
BRANCH_POLICIES = ("custom", "none")
# Positive grammars for the registry values a consumer resolves by exact comparison and then puts into a request path.
# A negative whitespace test cannot state any of these, because str.strip() removes the 29 characters Python calls whitespace and leaves U+200B, U+2060, U+FEFF and U+00AD standing.
# Each of those is invisible and each is exactly as unmatchable by an exact comparison as a trailing space, so refusing padding admits the zero-width twin that fails in the same place for the same reason.
# Stated positively a grammar admits nothing invisible, needs no notion of whitespace at all, and cannot drift when a Python release changes what str.isspace() answers.
# Each pattern below means the same thing in Python re and in ECMA-262, the engine an editor resolving registry/repos.schema.json actually uses.
# That schema therefore carries these exact strings, and scripts/tests/test_spec_validate.py asserts that it still does.
# The portability is what makes an editor-side copy safe at all, since #1504 reverted an earlier attempt for leaving the editor refusing values the gate allowed.
# `$` is end-of-string in only one of the two, since Python's also matches just before a trailing newline.
# `(?![\s\S])` is end-of-string in both, because `\s` and `\S` name different sets in the two engines while their union is every character in each.
# One line of printable ASCII with no leading or trailing space, for a deployment environment `name`.
# The check mode in repo-config/configure.sh resolves that name with `select(.name == $n)` and then percent-encodes it into one URL path segment.
# An interior space is admitted, since GitHub documents no character restriction on the name beyond length and uniqueness and nothing downstream splits the value on one.
# The ASCII floor is this fleet's rule rather than GitHub's, per GOVERNANCE.md "Documentation Style Conventions" and its "Character Set" rule: a non-ASCII environment name is legal on GitHub and repo-config/configure.sh percent-encodes one correctly, and it is refused here because the registry is agent-authored text and an environment name is an identifier compared exactly rather than prose read by a person.
# That is why the `description` field's own pattern stays looser: its tier-2 and tier-3 characters carry meaning their ASCII form loses, where an identifier's do not.
# A repo that genuinely needs one is a decision for the maintainer, and a refusal naming the shape is a better place to raise it than a silent mismatch against the live environment.
ENVIRONMENT_NAME_PATTERN = r"^[!-~](?:[ -~]*[!-~])?(?![\s\S])"
# A deployment branch policy name is a ref pattern rather than a ref, so `releases/*` is a legitimate declaration.
# This admits every visible ASCII character rather than the alphabet the three names the registry declares today happen to use.
# A grammar fitted to those three would pass every test and every live value, and refuse the first adopter declaring a release line.
# The space is the one printable character excluded, since a git ref name cannot carry one.
DEPLOYMENT_BRANCH_PATTERN = r"^[!-~]+(?![\s\S])"
# A ground-truth branch name reaches spec/audit.py raw, as the `{ground}` in `repos/{slug}/branches/{ground}` and as the value of a `?ref=` query parameter.
# So it admits only the characters that are both unreserved in RFC 3986 and legal in a git ref name, plus the `/` a branch name may carry.
# A `?` or a `#` there would re-parse the URL into a different request rather than fail, which is why the url field's own grammar excludes both and why this one has to as well.
# `..` is refused for the same reason and it is the sharpest case: measured against the live API, `branches/a/../../../../../zen` returns GitHub's /zen body with a 200, so a value the gate accepted would have this repository's ground-truth head read out of a different endpoint entirely.
# The leading lookahead is what refuses it, since a positive character class cannot say "no two of these adjacent" and `(?![\s\S]*\.\.)` is a whole-string negation both engines read identically.
# `~` is excluded although RFC 3986 calls it unreserved, because `git check-ref-format refs/heads/~x` rejects it, so admitting it would refuse nothing and promise a name no repository can hold.
# Neither end may be a `.` or a `/`, which is git's rule rather than a whitespace one, and every other admitted character is allowed at either end so that `_wip` and `wip-` both pass.
GROUND_TRUTH_BRANCH_PATTERN = (
    r"^(?![\s\S]*\.\.)[A-Za-z0-9_-](?:[A-Za-z0-9._/-]*[A-Za-z0-9_-])?(?![\s\S])"
)
# The one statement of the grammar in words, so the message spec/audit.py prints for its `--branch` override cannot drift from the one printed here.
GROUND_TRUTH_BRANCH_SHAPE = (
    "ASCII letters, digits, and . _ - /, with neither end a . or a /, and no .."
)
# A `requiredSecrets` name is resolved against the repository actions store by an exact comparison, so it is the same class of value as the three above and takes the same treatment.
# This is GitHub's own rule for a secret name rather than a shape fitted to the four names the registry declares today: alphanumerics and underscores, and not opening with a digit.
# Naming GitHub's set refuses no name that can be stored, where a grammar fitted to the current values would pass every test and refuse the first repo declaring a secret those four happen not to use.
SECRET_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*(?![\s\S])"
SECRET_NAME_SHAPE = "ASCII letters, digits and _, not opening with a digit"
ENVIRONMENT_NAME_RE = re.compile(ENVIRONMENT_NAME_PATTERN)
DEPLOYMENT_BRANCH_RE = re.compile(DEPLOYMENT_BRANCH_PATTERN)
GROUND_TRUTH_BRANCH_RE = re.compile(GROUND_TRUTH_BRANCH_PATTERN)
SECRET_NAME_RE = re.compile(SECRET_NAME_PATTERN)
# Parses owner/repo, lowercased, from a repo's url.
# A trailing .git is stripped so it still matches GitHub's own full_name.
# A query character or a fragment character is excluded from both groups too.
# Otherwise a query string or fragment folds into the repo name instead of failing to match.
# A duplicate identity here would let spec/audit.py's fleet membership check silently shadow one entry with the other.
# `(?![\s\S])` rather than `$`, for the reason given below at the grammars.
# `$` also matches just before a trailing newline in Python, so a newline-padded url parsed here while a space-padded one did not, and the padding reached spec/audit.py's request path either way.
# The two segments name GitHub's own character sets rather than "anything that is not a delimiter".
# GitHub restricts an owner to letters, digits and hyphens and a repository name to letters, digits, `.`, `_` and `-`, replacing anything else at creation time, so naming those sets refuses no url that can exist.
GITHUB_URL_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?(?![\s\S])"
)
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


def _bracket_matches(text, open_char, close_char):
    """Map from each open_char index in text to the index just past its balanced close_char.

    A character class like `[^\\]]*` cannot count depth, so it stops at the first close and misses a link
    label carrying its own nested brackets, e.g. `[API [docs]](url)`. Counting only open_char/close_char
    nesting, ignoring the other bracket type, needs one such map per bracket type rather than one pass
    mixing both. Built with a single left-to-right stack pass over the whole text rather than one depth-
    counting scan per open position: re-scanning from every unmatched open is what made a prior version of
    this walk O(N^2) on a run of N unmatched opens (#1011, CodeRabbit, on spec/audit.py's sibling
    implementation). A close pops the most recently pushed open, the same pairing a fresh depth count from
    that open would find, so this is one pass, not N. An open with no closing partner, or a close with
    nothing open, is left out of the map, same as before.

    A backslash-escaped delimiter (`\\[`, `\\]`, `\\(`, `\\)`) is skipped rather than pushed or popped,
    matching Markdown's own escaping rule, so a literal bracket inside a label does not corrupt the nesting
    count (#1011, qodo).
    """
    stack = []
    matches = {}
    escaped = False
    for i, c in enumerate(text):
        if escaped:
            escaped = False
        elif c == "\\":
            escaped = True
        elif c == open_char:
            stack.append(i)
        elif c == close_char and stack:
            matches[stack.pop()] = i + 1
    return matches


def contains_description_markdown_link(text):
    """Whether `text` carries a `[text](url)` or `[text][ref]` use, brackets/parens balanced.

    Kept in sync with spec/audit.py's markdown_link_spans(), which needs the same balanced-nesting rule for
    the same reason: this is a description-shaped link use inside one short string, not the carried-link
    regexes above, which find a definition's target inside a whole document.
    """
    bracket_close = _bracket_matches(text, "[", "]")
    paren_close = _bracket_matches(text, "(", ")")
    i, n = 0, len(text)
    while i < n:
        if text[i] != "[":
            i += 1
            continue
        label_end = bracket_close.get(i)
        if label_end is None:
            i += 1
            continue
        if label_end < n and text[label_end] == "(" and label_end in paren_close:
            return True
        if label_end < n and text[label_end] == "[" and label_end in bracket_close:
            return True
        # This span is not itself a link.
        # A nested bracket run starting inside it may still be one, e.g. `[[docs](url)]`.
        # Retry one character in rather than skipping past the whole span (#1011, qodo).
        i += 1
    return False


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def is_str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def escapes_repo_root(value):
    """Whether `ROOT / value` could resolve outside ROOT on some host.

    `PurePosixPath` alone misses a backslash (Windows treats it as a separator, though POSIX reads it as one filename) and a Windows drive letter such as `C:`.
    """
    return (
        not value
        or value.startswith("/")
        or "\\" in value
        or re.match(r"^[A-Za-z]:", value) is not None
        or ".." in pathlib.PurePosixPath(value).parts
    )


def reduces_to_repo_root(value):
    """Whether `value` reduces to the repository root under POSIX path rules.

    A raw-string compare against "." accepts "./" and ".///.", which the schema also allows and which `scripts/carry.py` then refuses, so a manifest author gets a green validator and a failing carrier. Backslash and drive-letter spellings are `escapes_repo_root`'s subject and are not read here.
    """
    return pathlib.PurePosixPath(value) == pathlib.PurePosixPath(".")


def canonical_file_in_root(rel_path):
    """Whether `ROOT / rel_path` resolves, symlinks followed, to an existing file under ROOT.

    A tracked symlink whose target escapes ROOT passes a bare `Path.is_file()` the same way a real file would, since both follow the link.
    """
    try:
        resolved = (ROOT / rel_path).resolve(strict=True)
    except OSError:
        return False
    return resolved.is_file() and resolved.is_relative_to(ROOT)


def environment_errors_for_repo(repo, name):
    """Shape errors for a registry entry's optional `environments` (a repo using no deployment environment declares none).

    Absence is not checked, since most of the fleet uses no environment. What is checked is that a declared entry
    carries the `name` and `branchPolicy` that repo-config/configure.sh's check mode always reads, and that
    `branches` is present exactly when `branchPolicy` is "custom", which that check mode reads in both directions.
    "none" names no branch set, since it admits every ref, so a `branches` beside it would be a declaration
    nothing compares against.

    Presence (`"branches" in env`) is the test rather than truthiness, so a declared empty list under "custom" is
    read as declaring that the environment allows nothing, which configure.sh then asserts, rather than as absent.
    Presence is the test for the field itself too, matching description_errors_for_repo: an explicit
    `"environments": null` is declared-but-invalid, not absent, so it reaches the list check below rather than
    passing as a repo that declares none.
    """
    if "environments" not in repo:
        return []
    envs = repo["environments"]
    if not isinstance(envs, list):
        return [f"{name}: environments must be a list"]
    errors = []
    seen = set()
    for i, env in enumerate(envs):
        where = f"{name}: environments[{i}]"
        if not isinstance(env, dict):
            errors.append(f"{where} must be an object")
            continue
        env_name = env.get("name")
        if not isinstance(env_name, str) or not env_name:
            errors.append(f"{where} missing or empty 'name'")
        # The grammar rather than a padding test, per ENVIRONMENT_NAME_PATTERN, because what reaches the consumer depends on which padding it is.
        # A trailing space survives into a name that matches no live environment, a trailing newline is eaten by command substitution on the way, and a zero-width space fails exactly like the first while no whitespace test names it.
        # A blank name lands here rather than on the branch above, since it is a non-empty string of the wrong shape and reading it as absent needs the notion of whitespace this grammar exists to drop.
        elif not ENVIRONMENT_NAME_RE.search(env_name):
            errors.append(
                f"{where} name {env_name!r} is not one line of printable ASCII with no leading or trailing space"
            )
        elif env_name in seen:
            # Two entries for one environment would have configure.sh assert the same live state twice, against declarations that may disagree.
            errors.append(f"{where} duplicate environment '{env_name}'")
        else:
            seen.add(env_name)
        # Absence is reported as absence rather than through the value branch, whose repr of a missing field differs from the valid value "none" by case alone.
        if "branchPolicy" not in env:
            errors.append(f"{where} missing 'branchPolicy' (expected {', '.join(BRANCH_POLICIES)})")
            continue
        policy = env["branchPolicy"]
        if policy not in BRANCH_POLICIES:
            errors.append(
                f"{where} branchPolicy {policy!r} invalid (expected {', '.join(BRANCH_POLICIES)})"
            )
            continue
        branches = env.get("branches")
        if policy == "custom":
            if "branches" not in env:
                errors.append(f"{where} branchPolicy custom must declare 'branches'")
            elif not isinstance(branches, list) or not all(
                isinstance(b, str) and b for b in branches
            ):
                errors.append(f"{where} branches must be a list of non-empty strings")
            # A declared branch name reaches its comparison the same way, so it takes a grammar of its own for the reason given at DEPLOYMENT_BRANCH_PATTERN rather than one borrowed from the name above.
            elif malformed := [b for b in branches if not DEPLOYMENT_BRANCH_RE.search(b)]:
                errors.append(
                    f"{where} branches {malformed!r} are not printable ASCII with no spaces"
                )
            # Both sides are sorted and joined by configure.sh before comparing, so a name declared twice makes the joined declaration longer than any live set can be and reports as drift on an environment that has none.
            elif duplicates := sorted(
                b for b, count in collections.Counter(branches).items() if count > 1
            ):
                errors.append(f"{where} branches {duplicates!r} are declared more than once")
        elif "branches" in env:
            errors.append(
                f"{where} branchPolicy {policy} names no branch set, so it must not declare 'branches'"
            )
    return errors


def ground_truth_branch_errors_for_repo(repo, name):
    """Shape errors for a registry entry's optional `groundTruthBranch` (a repo reading "main" declares none).

    Presence is the test rather than truthiness, matching description_errors_for_repo, because spec/audit.py's
    ground_branch_of() also defaults only on absence: `entry.get("groundTruthBranch", "main")` returns the empty
    string for a declared `""`, which is then addressed rather than replaced by "main".

    spec/audit.py, spec/fidelity_honesty.py and spec/workflow_reuse.py each concatenate the value straight into a
    request path and a `?ref=` query value, so the grammar is what makes the declared value and the addressed one
    the same string.
    """
    if "groundTruthBranch" not in repo:
        return []
    ground = repo["groundTruthBranch"]
    if not isinstance(ground, str) or not GROUND_TRUTH_BRANCH_RE.search(ground):
        shape = GROUND_TRUTH_BRANCH_SHAPE
        return [f"{name}: groundTruthBranch {ground!r} does not address unencoded ({shape})"]
    return []


def description_errors_for_repo(repo, name):
    """The per-repo optional-field guard: an explicit `"description": null` is declared-but-invalid, not absent.

    Presence (`"description" in repo`) is the test, not `repo.get("description") is not None`, so a `null` reaches
    description_errors() rather than being read as though the field were never declared.
    """
    if "description" not in repo:
        return []
    return description_errors(name, repo["description"])


def description_errors(name, desc):
    """Shape errors for a registry entry's optional `description` (GOVERNANCE.md "Repository Details").

    Absence is not checked here, since the field is optional. `desc` is only passed in once a repo declares it.

    Link-free is enforced because spec/audit.py's description_findings() strips Markdown links from the README's
    own tagline before comparing, but never re-strips the declared field it compares that tagline against. A
    declared value carrying a link would therefore report as a permanent readme mismatch, and repo-config/
    configure.sh would push the literal Markdown source to GitHub's About panel, which does not render it.

    Already trimmed and single-line is enforced so the registry's own text already reads as exactly what every
    mirror carries, rather than relying on the whitespace-stripping spec/audit.py and repo-config/configure.sh
    each do defensively to keep agreeing with each other.
    """
    if not isinstance(desc, str) or not desc.strip():
        return [f"{name}: description must be a non-empty string"]
    if desc != desc.strip() or "\n" in desc or "\r" in desc:
        return [
            f"{name}: description must be plain single-line text with no leading or trailing whitespace"
        ]
    if contains_description_markdown_link(desc):
        return [f"{name}: description carries Markdown links - keep it link-free plain text"]
    if len(desc) > 100:
        return [f"{name}: description is {len(desc)} characters, over the 100-char limit"]
    return []


def github_identity(url):
    """The `<owner>/<repo>` spec/audit.py addresses, or None where the url is not one this fleet can address.

    One definition rather than two. spec/audit.py's repo_slug() took the last two path segments of the raw value, so
    a trailing `.git`, which GITHUB_URL_RE makes optional and strips for the identity, survived into every
    `repos/{slug}/...` request, and a padded url passed a check made on `.strip()` while the request kept the
    padding. A validator can only be as strict as its consumer where the two read the value the same way, so the
    consumer calls this rather than re-deriving a looser answer of its own.

    Case is preserved, since the slug is what the audit prints and addresses. The identity comparison that dedupes
    the registry lowercases this result itself, at the one place that needs a case-insensitive answer.
    """
    match = GITHUB_URL_RE.match(url) if isinstance(url, str) else None
    if match is None:
        return None
    owner, repo = match.group(1), match.group(2)
    # GitHub decodes and then normalizes a dot segment, so a segment that is nothing but dots is refused rather than addressed.
    # The owner class above already refuses one, which is the position it mattered in: measured against the live API, `repos/../rate_limit` returned 200 with the rate-limit document and `repos/../..` returned the API root, and percent-encoding is no defense since `repos/%2e%2e/rate_limit` returned 200 the same way.
    # The repo class admits `.`, so only that position reaches this check, and it is refused for a narrower reason: `repos/ptr727/../rate_limit` returned 404 rather than a different document, but the normalization eats a segment either way, so what a read addresses stops being decided by the declaration and starts being decided by the shape of that read.
    # The check is here rather than in the pattern because a character class cannot say "not only dots".
    # Only `.` and `..` normalize, so refusing a segment that is nothing but dots is the whole of it, and `..a`, `.github` and `v1.0` are untouched.
    if repo.strip(".") == "":
        return None
    return f"{owner}/{repo}"


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

    # Checked here because registry/repos.schema.json holds this key to the same grammar the per-repo field uses.
    # An advisory schema stricter than the gate is the direction #1504 was reverted for.
    errors.extend(ground_truth_branch_errors_for_repo(reg_defaults, "defaults"))

    # Both the defaults object and a repo entry are marked `additionalProperties: false` in registry/repos.schema.json, and no gate runs that schema, so a misspelled key passes CI today.
    # It fails silently rather than loudly: a repo declaring `groundTruthBanch` validates clean and the audit reads main without ever saying that the declaration it was given went unread.
    # The key set is read out of the schema rather than restated here, so the rule has one statement.
    # A second list in this file would drift from the first in the direction nobody checks, since a key added to the schema and not to the list would be reported as unknown on the entry that legitimately declares it.
    # The two objects the registry's own consumers index are the ones checked.
    # A nested object is left to the schema, and to the per-field checks that already read it.
    schema = load("registry/repos.schema.json")
    try:
        repo_keys = set(schema["$defs"]["repo"]["properties"])
        defaults_keys = set(schema["properties"]["defaults"]["properties"])
    except (KeyError, TypeError):
        # A schema this file cannot read is reported rather than skipped, since skipping would leave the unknown-key check silently absent, which reads exactly like a registry with no unknown keys.
        errors.append(
            "repos.schema.json: cannot read the declared property names for 'defaults' and a repo entry, so no unknown key could be checked"
        )
        repo_keys, defaults_keys = None, None
    if defaults_keys is not None:
        stray = sorted(set(reg_defaults) - defaults_keys)
        if stray:
            errors.append(
                f"defaults: unknown key(s) {', '.join(stray)} - registry/repos.schema.json declares none of them, so nothing reads the value"
            )

    seen_identities = set()
    seen_names = {}
    for i, repo in enumerate(repos["repos"]):
        if not isinstance(repo, dict):
            errors.append(f"repo #{i} is not an object")
            continue
        name = repo.get("name", f"#{i}")
        # This name only labels every error message below.
        # The membership check (spec/audit.py's membership_findings()) keys by owner/repo instead, parsed from url the same way this loop does.
        # A duplicate is still an error, though, since repo-config/configure.sh and spec/audit.py's own per-repo entry lookup both key off it.
        if not isinstance(repo.get("name"), str) or not repo["name"].strip():
            errors.append(f"repo #{i}: missing or empty 'name'")
            continue
        if name != name.strip():
            # Both configure.sh and audit.py key their per-repo lookup off an exact match on name.
            # A padded value would therefore make the entry unresolvable there, not merely cosmetic here.
            errors.append(f"repo #{i}: name '{name}' carries leading/trailing whitespace")
            continue
        # Casefolded rather than exact, because spec/audit.py narrows to a named repo on the same casefold.
        # Two entries differing only in case would otherwise pass here and then both answer one --repo, with nothing reporting the collision.
        # The entry already declared is named rather than qualified as case-differing, since the two shapes read the same way and only one of them is.
        if name.casefold() in seen_names:
            first = seen_names[name.casefold()]
            errors.append(
                f"{name}: duplicate registry entry for name '{name}', already declared as '{first}'"
            )
        seen_names.setdefault(name.casefold(), name)
        if not isinstance(repo.get("url"), str) or not repo["url"]:
            errors.append(f"{name}: missing or empty 'url'")
            continue
        # Matched on the raw value rather than on `.strip()`, because spec/audit.py's repo_slug() builds its request path from what the registry declares.
        # A value accepted here after trimming would be the string checked here and a different string requested there.
        # A blank url reaches this error rather than the one above for the same reason the environment name does, since it is a non-empty string of the wrong shape.
        # A url that is a well-formed URI but not this exact shape (http://, a path suffix) would otherwise pass here.
        # It would only surface later as a false DEFECT, since membership_findings() can never resolve it to an identity.
        slug = github_identity(repo["url"])
        if slug is None:
            errors.append(f"{name}: url is not a github.com/<owner>/<repo> URL")
            continue
        identity = slug.lower()
        if identity in seen_identities:
            errors.append(f"{name}: duplicate registry entry for '{identity}'")
        seen_identities.add(identity)
        # The key and the identity must name the same repository, since repo-config/configure.sh derives its lookup key from the repo argument, `name="${repo##*/}"`, rather than from the registry.
        # An entry whose name differs from its url's repo segment resolves to nothing there, and every consequence is a silent one: the environment assertions are skipped while the run still reports no drift, the description lookup degrades the same way, and the workflow-model lookup falls through to defaults.workflowModel, so a repo declared operational is checked against the release ruleset payload.
        # Compared against github_identity()'s segment rather than against the raw url, so this reads the same string spec/audit.py addresses, with an optional trailing `.git` already stripped.
        # Agreement is also the whole of the name's own grammar, and deliberately so.
        # GITHUB_URL_RE holds the repo segment to the letters, digits, `.`, `_` and `-` GitHub itself allows, so a name that equals one carries no invisible character and needs no pattern of its own to say so.
        # A separate grammar was the other candidate and was rejected for being a second statement of the same shape, free to drift from the first, where this one cannot disagree with the url it is read from.
        url_name = slug.split("/", 1)[1]
        if name != url_name:
            errors.append(
                f"{name}: name and url disagree, the url naming repo '{url_name}' - repo-config/configure.sh keys the registry on the url's segment, so this entry resolves to nothing there"
            )

        # Checked per entry for the reason given at the defaults check above, and on every status, since a backlog or archived entry is read by the fleet membership check and by whatever promotes it later.
        if repo_keys is not None:
            stray = sorted(set(repo) - repo_keys)
            if stray:
                errors.append(
                    f"{name}: unknown key(s) {', '.join(stray)} - registry/repos.schema.json declares none of them, so nothing reads the value"
                )

        # These fields are facts about the repo itself, not about its audit scope.
        # The schema's operational-needs-lineEndings rule (registry/repos.schema.json) binds regardless of status.
        # Checked here, before the status branch, so every status shares one check rather than each non-cataloged branch needing its own copy.
        model = repo.get("workflowModel")
        if model is not None and model not in WORKFLOW_MODELS:
            errors.append(
                f"{name}: workflowModel '{model}' invalid (expected {' or '.join(WORKFLOW_MODELS)})"
            )
        errors.extend(ground_truth_branch_errors_for_repo(repo, name))
        eol = repo.get("lineEndings")
        if eol is not None and eol not in ("lf", "crlf"):
            errors.append(f"{name}: lineEndings '{eol}' invalid (expected lf or crlf)")
        # An operational repo's endings follow the consuming app's platform, so they must be declared, where a release repo omits the field and takes the fleet LF default.
        # Resolve the effective model the way configure.sh does, from the repo, then the defaults, then release.
        # The requirement then holds even where a repo relies on an operational defaults.workflowModel rather than setting its own.
        effective_model = model or default_model or "release"
        if effective_model == "operational" and eol is None:
            errors.append(f"{name}: operational repo must declare lineEndings (lf or crlf)")
        # Read by spec/audit.py as `bool(entry.get("hasDevelop"))` and compared against the live branch, so the coercion decides the answer wherever the declared value is not already a boolean.
        # The string "no" reads as True and an empty list reads as False, and the DRIFT line prints the raw value either way, so the report would name a value that is not what was compared.
        # Presence is the test rather than truthiness, since a declared `false` is the answer for a repo that has no develop branch and must not be read as an undeclared field.
        if "hasDevelop" in repo and not isinstance(repo["hasDevelop"], bool):
            errors.append(
                f"{name}: hasDevelop {repo['hasDevelop']!r} must be true or false, and spec/audit.py coerces whatever is declared"
            )
        # Compared exactly against the names GitHub stores, by spec/audit.py's secret audit and by this file's own requires/forbids cross-check below.
        # A padded element is therefore reported missing from the actions store on every run while the unpadded name it was meant to be goes unrequired, and a non-string element reaches a set membership test that answers False for every name there is.
        # A positive grammar rather than a padding test, per the grammars at the top of this file, and it is GitHub's own rule for a secret name: letters, digits and underscores, not opening with a digit.
        secrets_decl = repo.get("requiredSecrets")
        if secrets_decl is not None:
            if not isinstance(secrets_decl, list):
                errors.append(f"{name}: requiredSecrets must be an array of secret names")
            else:
                for s in secrets_decl:
                    if not isinstance(s, str) or not SECRET_NAME_RE.search(s):
                        errors.append(
                            f"{name}: requiredSecrets entry {s!r} is not a GitHub secret name ({SECRET_NAME_SHAPE})"
                        )
        # Each note is sliced and run through a regex by spec/audit.py to resolve the check ids it names, so a non-string element raises there mid-run rather than reporting.
        # An audit that raises reports nothing at all, where the malformed note it choked on would have been one line.
        notes_decl = repo.get("driftNotes")
        if notes_decl is not None:
            if not isinstance(notes_decl, list):
                errors.append(f"{name}: driftNotes must be an array of notes")
            else:
                for note in notes_decl:
                    if not isinstance(note, str) or not note.strip():
                        errors.append(
                            f"{name}: driftNotes entry {note!r} must be a non-empty string"
                        )
        # Optional per GOVERNANCE.md "Repository Details": a repo that has not adopted the field yet is unaffected, since spec/audit.py's description_findings() falls back to the README tagline for it.
        errors.extend(description_errors_for_repo(repo, name))
        # Optional, and read by repo-config/configure.sh's check mode rather than by any check here.
        # CI runs no JSON-schema validation, so the schema's own required-fields and branches-iff-custom rules are enforced here or nowhere.
        # A malformed entry would otherwise reach configure.sh, where a missing branchPolicy asserts against the literal "null" jq prints and reports drift on a repo that has none.
        errors.extend(environment_errors_for_repo(repo, name))

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

        # Required on a cataloged repo, which is the set spec/audit.py audits, because there an absent field is not read as undeclared.
        # It is coerced to False and compared against the live branch, so omitting it asserts that the repo has no develop branch rather than declining to say.
        # The type check above holds wherever the field is declared, and this is the one status that must declare it.
        if "hasDevelop" not in repo:
            errors.append(
                f"{name}: cataloged repo must declare hasDevelop, since spec/audit.py reads an absent field as false and audits the branch against it"
            )

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
            if reduces_to_repo_root(value) or value.startswith("/") or ".." in parts:
                errors.append(
                    f"files.json: tree {source} {field} '{value}' must be a path below the repository root"
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
        elif isinstance(ref, str):
            if escapes_repo_root(ref):
                errors.append(f"files.json: {path} reference '{ref}' must be a repo-relative path")
            elif fid == "intent" and not canonical_file_in_root(ref):
                # This field outranks intentRef in the audit engine's canonical resolution.
                # An intent unit's reference needs the same existing-file check intentRef gets below.
                errors.append(
                    f"files.json: {path} reference '{ref}' is not a file in this checkout"
                )
        if fid == "verbatim":
            src = ref if isinstance(ref, str) else path
            if isinstance(src, str) and not (ROOT / src).exists():
                errors.append(
                    f"files.json: {path} fidelity 'verbatim' but its canonical source {src} is missing"
                )

        # The audit engine's intent_canonical_rel() trusts this is a string once validated, the same way it trusts reference above.
        intent_ref = item.get("intentRef")
        if intent_ref is not None and not isinstance(intent_ref, str):
            errors.append(f"files.json: {path} intentRef must be a string")
        elif isinstance(intent_ref, str):
            # The audit engine strips a trailing #anchor before ever joining this with ROOT, so validate the same part it will actually read.
            intent_path = intent_ref.split("#", 1)[0]
            if escapes_repo_root(intent_path):
                errors.append(
                    f"files.json: {path} intentRef '{intent_ref}' must be a repo-relative path"
                )
            elif not canonical_file_in_root(intent_path):
                # A directory such as "." exists but is not a file.
                # The staleness check would then read the whole repo's most recent commit as this one file's, false-flagging every intent unit as stale.
                errors.append(
                    f"files.json: {path} intentRef '{intent_ref}' canonical {intent_path} is not a file in this checkout"
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

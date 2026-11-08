#!/usr/bin/env bash
# Configure or validate a repository against the committed fleet config in this directory, via the GitHub API.
#
#   Apply:  repo-config/configure.sh apply [owner/repo] [release|operational] [--checkout <path>] [--dry-run]   # create-or-update settings + labels + rulesets + the fleet project link + secrets (writes), and with --checkout the agent policy files
#   Check:  repo-config/configure.sh check [owner/repo] [release|operational]   # validate an existing repo, non-zero on drift (reads)
#
# Both modes need admin on the repo, because the rulesets endpoints require it.
# Both modes also need project access on the token, because the fleet project link is read and written through GraphQL.
# The command is required, so a write is never the default, and an argument the usage does not name is refused before any read.
# The repo defaults to the current gh repo, and the model to the registry lookup.
# The model may be passed as the sole positional, as in `configure.sh check operational`.
#
# The apply mode writes five groups, in order.
# It adds the agent policy files when --checkout names a local checkout of the repo.
# A last group of secrets follows.
# First settings.json via PATCH, plus has_discussions (public repos only) and default_branch (main, only when it exists).
# Then the Dependabot vulnerability alerts and automated security updates.
# Then the fleet label set from labels.json, create-or-update by name, leaving any label the payload does not declare alone.
# Then the branch rulesets, main.json shared and the model-specific develop ruleset, create-or-update by name.
# Then the link to the fleet project declared in project.json, written only when the live link list does not already hold it.
# The develop ruleset is develop.json where the model is PR-gated, or operational/develop.json for direct signed pushes.
# The agent policy files render the repo's .github/agent-policy.json from the registry through scripts/agent_policy.py.
# It writes that file plus the CodeRabbit and Qodo settings derived from it into the checkout, and never stages or commits.
# The caller commits the files through the ordinary pull request flow.
# The same render decides whether each ruleset carries a Copilot code review rule, so a repo whose policy has no automatic Copilot gets no rule.
# The registry's pullRequestCheck decides whether each ruleset requires the pull request status check, so a repo whose workflow cannot post it gets no rule.
# Deriving the policy needs only Python, while writing the files and checking them run under uv.
# The checkout is planned before the first API write, so a checkout that refuses stops the run with nothing applied.
# Without --checkout apply writes no policy files and says so.
# The secrets group runs last, after the agent policy files, and sets each secret name the repo owes from the host's ~/.secrets/ directory.
# The names come from spec/resolve_secrets.py, which uses the resolver spec/audit.py uses and reads the tree of the registry's ground branch.
# Each name is scoped in spec/secrets.json, and a fleet name is read from github_<name> while a repository name is read from github_<repo>_<name>.
# Only the file a name's scope names is read.
# A file at the wrong scope is a stray that stops the run before any write.
# Every name whose file is present is written to every store that needs it on every run.
# The value goes on stdin and never in an argument.
# A name with no file and a file the secrets library refuses are each reported in their own list.
# Either one makes the run exit non-zero after the other secrets are set.
# A secret on GitHub that nothing claims is never deleted, and the check mode reports it.
# The --dry-run option runs the whole pre-flight and prints what the secrets group would set, writing nothing, and the other groups have no dry run.
# Applying the same configuration twice changes nothing, so the mode is idempotent.
#
# The check mode is the read-only inverse, and it verifies the same five groups apply writes.
# The static-settings assertions are driven by the committed payloads, so they stay repo-agnostic.
# The ruleset assertions compare against the payloads composed for the repo, the same ones apply writes.
# A ruleset is checked on enforcement, on the rule-type set compared in both directions, and on the whole parameters object of every parameterized rule.
# Comparing the parameters object rather than named fields means a parameter added to a payload is audited with no change here.
# Both directions matter, since a rule added live that the payload never declared is drift this catches.
# That still survives the GitHub API normalizing a stored ruleset, since the comparison is over parsed JSON with sorted keys rather than a byte diff.
# The derived settings apply computes are asserted by name rather than from a payload, meaning has_discussions and default_branch.
# The two Dependabot security features are asserted the same way, since apply enables them and no payload declares them.
# The agent policy files are checked too, read from the repo's default branch and compared with the render.
# The project link is checked by resolving the declared project to a node id.
# The check then asserts that id is among the ones the repository is linked to.
# A label is checked on name, color, and description against labels.json.
# A label the payload never declared is reported without being asserted, since a repo may carry labels of its own.
# What is unaudited is a static setting absent from settings.json and a label labels.json does not declare.
# That is because those two groups are asserted in the payload's direction only, where a ruleset's rule-type set is compared both ways.
# The check mode also asserts one group apply never writes, the existence and deployment-branch policy of every environment the registry declares for the repo.
# It is check-only because an environment's secrets and variables are credentials that no group here provisions.
# An apply would therefore create an environment that cannot publish and report success.
# Their names are enumerable through the API, and a variable's value too.
# What this script does not check is therefore a choice about what it owns rather than a limit the API imposes.
# The policy is worth checking on its own because it decides which refs may deploy at all.
# It is also worth checking because, on an OIDC publish, that is half the gate: a ref the policy excludes mints no token.
# A repo declaring no environment has nothing asserted against it, though the live list is still read and an unreadable one fails the run.
# An environment the registry does not declare is reported rather than asserted, since GitHub creates some on its own.
# The check mode also asserts the repository-level secret names in both stores, which are the ones the secrets group writes.
# A name the repo owes must be present, a forbidden name must be absent, and a name nothing claims is reported and never asserted.
# Environment-scoped secrets are neither written nor checked here, since they depend on the environment provisioning that has not landed.
# A secret's value is never readable via the API, so a present name says nothing about whether its value is right.
set -Eeuo pipefail

# ----- Command + target + model -----
usage() {
    cat <<'USAGE'
Usage: repo-config/configure.sh apply|check [owner/repo] [release|operational]
       repo-config/configure.sh apply|check release|operational
       repo-config/configure.sh apply [owner/repo] [release|operational] --checkout <path>
       repo-config/configure.sh apply [owner/repo] [release|operational] --dry-run
       repo-config/configure.sh --help

  apply   create or update the fleet configuration on the repo (writes)
  check   validate the repo against the fleet configuration, non-zero on drift (reads)

  --checkout <path>   with apply only, also write the agent policy files into this local checkout of the repo
  --dry-run           with apply only, run the pre-flight and print the secrets that would be set, writing nothing

The repo defaults to the current gh repo, and the model to the registry lookup.
USAGE
}
refuse() {
    echo "$1. Run repo-config/configure.sh --help for the usage." >&2
    exit 1
}
case "${1:-}" in
-h | --help)
    usage
    exit 0
    ;;
apply | check)
    cmd="$1"
    shift
    ;;
"") refuse "No command given (expected apply or check)" ;;
*) refuse "Unknown command '$1' (expected apply or check)" ;;
esac
# The option is parsed ahead of the positionals, so it may sit before or after them.
checkout=""
checkout_given=false
dry_run=false
positional=()
while [ "$#" -gt 0 ]; do
    case "$1" in
    -h | --help)
        usage
        exit 0
        ;;
    --checkout)
        if [ "$#" -lt 2 ] || [[ "$2" == -* ]]; then
            refuse "Option '--checkout' needs a path"
        fi
        checkout="$2"
        checkout_given=true
        shift 2
        ;;
    --checkout=*)
        checkout="${1#--checkout=}"
        checkout_given=true
        shift
        ;;
    --dry-run)
        dry_run=true
        shift
        ;;
    -*) refuse "Unknown option '$1'" ;;
    *)
        positional+=("$1")
        shift
        ;;
    esac
done
# An empty array expansion trips set -u on the oldest bash, so it is expanded only when it holds something.
if [ "${#positional[@]}" -gt 0 ]; then
    set -- "${positional[@]}"
else
    set --
fi
if [ "$checkout_given" = true ]; then
    if [ "$cmd" != apply ]; then
        refuse "Option '--checkout' is valid only with apply"
    fi
    if [ -z "$checkout" ]; then
        refuse "Option '--checkout' needs a path"
    fi
fi
if [ "$dry_run" = true ] && [ "$cmd" != apply ]; then
    refuse "Option '--dry-run' is valid only with apply"
fi
if [ "$#" -gt 2 ]; then
    refuse "Too many arguments (expected at most a repo and a model)"
fi
repo_arg="${1:-}"
model="${2:-}"
# Allow the model as the sole positional (`configure.sh check operational`): a model name is not a repo.
case "$repo_arg" in release | operational)
    if [ -n "$model" ]; then
        refuse "The model '$repo_arg' comes before the repo (expected the repo, then the model)"
    fi
    model="$repo_arg"
    repo_arg=""
    ;;
esac
# A name of only dots is refused too, since the API resolves `.` and `..` as path segments rather than as a repo.
if [ -n "$repo_arg" ]; then
    if ! [[ "$repo_arg" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]] || [[ "${repo_arg#*/}" =~ ^\.+$ ]]; then
        refuse "Repo '$repo_arg' is not shaped owner/name"
    fi
fi
case "$model" in
"" | release | operational) ;;
*) refuse "Unknown workflow model '$model' (expected release or operational)" ;;
esac
repo="${repo_arg:-$(gh repo view --json nameWithOwner --jq '.nameWithOwner')}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----- Resolve the workflow model (selects the develop ruleset), shared by apply and check -----
registry="$script_dir/../registry/repos.json"
# The registry key is the entry name rather than the owner/repo identity parsed from its url.
# Reviewers keep proposing the switch, and the answer is that a name resolves to at most one entry.
# The script spec/resolve_description.py raises on a duplicate or a case-only near-miss and exits this script below.
# The script spec/validate.py refuses both when it is run.
name="${repo##*/}"

if [ -f "$registry" ]; then
    if ! archived_status="$(jq -r --arg n "$name" '(.repos[] | select(.name==$n) | .status) // empty' "$registry" | sed $'s/\r$//')"; then
        echo "Failed to read status from $registry (invalid JSON?)." >&2
        exit 1
    fi
    if [ "$archived_status" = archived ]; then
        echo "$repo is archived in $registry. GitHub rejects a write to an archived repository, so $cmd makes none and stops here."
        exit 0
    fi
fi

if [ -z "$model" ]; then
    if [ -f "$registry" ]; then
        # Fail fast on a jq or parse error (a malformed registry) rather than silently applying the release default.
        # Silently defaulting would hide a lookup that actually broke.
        # A repo simply absent from the registry is not an error.
        # The expression falls back through defaults.workflowModel to "release", so jq still exits 0 with a value.
        if ! model="$(jq -r --arg n "$name" '(.repos[] | select(.name==$n) | .workflowModel) // .defaults.workflowModel // "release"' "$registry" | sed $'s/\r$//')"; then
            echo "Failed to read workflowModel from $registry (invalid JSON?). Pass the model explicitly (release|operational)." >&2
            exit 1
        fi
    else
        echo "Registry $registry not found. Run this script from a hub checkout at main or pass the model explicitly (release|operational)." >&2
        exit 1
    fi
fi
case "$model" in
release | operational) ;;
*)
    echo "Unknown workflow model '$model' (expected release or operational)." >&2
    exit 1
    ;;
esac
settings_file="$script_dir/settings.json"
labels_file="$script_dir/labels.json"
project_file="$script_dir/project.json"

# ----- Resolve a Python interpreter (shared by apply and check) -----
# The ruleset composition and the description both come from plain Python scripts, so neither needs uv.
# The name python3 is not universal.
# Native Windows can register a Microsoft Store stub under that name that resolves on PATH but fails when actually run.
# So this runs it rather than just checking PATH (docs/host-setup.md).
# The probe checks 3.10+, which is the oldest interpreter the policy render and resolve_description.py run on.
# That is because pr_review_policy builds a `str | None` alias at import, not because 3.10 is this repo's supported floor.
# The repository floor is 3.13 (spec/host-tools.json's python3 target, enforced on the host by scripts/host_gate.py).
# A too-old interpreter fails here with a clear message instead of a bare traceback from the script.
# The py launcher is probed first.
# It reaches a registered interpreter whatever is active, where a bare python3 goes through PATH and a virtual environment can answer there instead.
if py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >/dev/null 2>&1; then
    py_cmd=(py -3)
elif python3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >/dev/null 2>&1; then
    py_cmd=(python3)
else
    echo "No Python 3.10 or newer found (python3 or py -3), the oldest that runs this repo's Python scripts. This repo targets Python 3.13 (see docs/host-setup.md)." >&2
    exit 1
fi

# ----- Compose the ruleset payloads from the agent policy and the registry (shared by apply and check) -----
# The Copilot rule follows the repo's rendered policy, and the required status check rule follows the registry's pullRequestCheck.
# The file main.json stays the template for the Copilot rule's parameters.
# The compose needs only the interpreter above, since that path of the script imports nothing outside the standard library.
# A failed render stops here, before any write.
agent_policy="$script_dir/../scripts/agent_policy.py"
agent_policy_args=(--registry "$registry" --name "$name" --model "$model")
compose_dir="$(mktemp -d)"
trap 'rm -rf "$compose_dir"' EXIT
if ! "${py_cmd[@]}" "$agent_policy" rulesets "${agent_policy_args[@]}" --out-dir "$compose_dir"; then
    echo "Could not compose the ruleset payloads for $repo. Aborting before any write." >&2
    exit 1
fi
main_ruleset="$compose_dir/main.json"
develop_ruleset="$compose_dir/develop.json"

# ----- Resolve the declared description (optional, shared by apply and check) -----
# Absence keeps the About panel following the README.
description=""
if [ -f "$registry" ]; then
    # Delegates to spec/resolve_description.py rather than a third hand-rolled copy of description_errors().
    # A description that passes that check can never contain a newline, so command substitution has nothing to strip.
    if ! description="$("${py_cmd[@]}" "$script_dir/../spec/resolve_description.py" -- "$registry" "$name")"; then
        exit 1
    fi
fi

# ----- Secret names and the host files they are read from (shared by apply and check) -----
# The names a repo owes come from spec/resolve_secrets.py, which resolves them through the spec/secret_names.py code spec/audit.py uses.
# It reads the tree of the registry's ground branch, the branch the audit reads, so both start from the same tree.
# That comes before jqr is defined, so the lookup below spells out the jq and sed pair that jqr stands for.
# A repo with no registry entry owes nothing the registry can name.
# The secrets group is therefore skipped for it with a note rather than failing the other groups.
# That matches how an absent entry is treated for the model and the description above.
secrets_spec="$script_dir/../spec/secrets.json"
resolve_secrets="$script_dir/../spec/resolve_secrets.py"
secrets_registered=false
if [ -f "$registry" ]; then
    # shellcheck disable=SC2016  # $n is a jq --arg variable, not a shell expansion
    if ! secrets_entry_count="$(jq -r --arg n "$name" '[.repos[] | select(.name == $n)] | length' "$registry" | sed $'s/\r$//')"; then
        echo "Failed to read $registry to resolve the entry for $name." >&2
        exit 1
    fi
    if [ "$secrets_entry_count" -gt 0 ]; then
        secrets_registered=true
    fi
fi
# The secrets library is the hub's own copy, the sanctioned reader of a private secret file per GOVERNANCE.md "Repo-Scoped Secrets".
# shellcheck disable=SC1091  # The linter runs without -x, and the library is linted as a file of its own.
. "$script_dir/../lib/sh/secrets.sh"

# Resolve the secret names this repo owes, leaving the resolver's JSON in secret_names_json.
# The resolver reads the tree of the registry's ground branch through read-only gh calls.
# That keeps one tree reader, which is not restated here.
# A truncated or unreadable tree fails, since a partial one would drop the codecov file a repo with tests owes.
# It returns non-zero with its own message on any failure, so the caller decides whether that stops the run or reports a failed check.
load_secret_names() {
    if ! secret_names_json="$("${py_cmd[@]}" "$resolve_secrets" names --slug "$repo" -- "$secrets_spec" "$registry" "$name")"; then
        echo "Could not resolve the secret names for $repo from its tree." >&2
        return 1
    fi
}

# Read every secret this repo owes from the directory in $1 and sort each into ready, missing, or refused.
# A stray stops the run here, before any write, and a stray is never read.
# It is a file the secrets directory holds for a name at the scope spec/secrets.json does not declare for it.
# Either the file is bad or the scope changed in config, and neither is something to resolve by picking one of the two files.
# The directory is an argument so a test passes a temporary one and nothing repurposes HOME.
# Each name is read from exactly the file its scope names, through secret_set_verdict, with no fallback to the other scope.
# The library does not refuse an empty file, so a file holding no visible character is refused here as an unfilled placeholder.
# It exits rather than returns on a stray, because a caller testing a function's status would switch errexit off inside it.
# The result is tab separated lines in secret_ready (name, scope, file, stores), secret_missing (the same), and secret_refused (name, file, sentence).
plan_secrets() {
    local dir="$1" strays stray rows sname scope file stores verdict path found=""
    secret_ready=""
    secret_missing=""
    secret_refused=""
    strays="$(jqr '.strays[]' <<<"$secret_names_json")"
    while IFS= read -r stray; do
        if [ -z "$stray" ]; then continue; fi
        if [ -e "$dir/$stray" ] || [ -L "$dir/$stray" ]; then
            found+="  $dir/$stray"$'\n'
        fi
    done <<<"$strays"
    if [ -n "$found" ]; then
        echo "Secret files at the wrong scope were found for $repo. None was read or changed. Remove the one that is not meant to be there, or fix its scope in spec/secrets.json, then run again:" >&2
        printf '%s' "$found" >&2
        echo "Aborting before any write." >&2
        exit 1
    fi
    rows="$(jqr '.rows[] | [.name, .scope, .file, (.stores | join(","))] | @tsv' <<<"$secret_names_json")"
    while IFS=$'\t' read -r sname scope file stores; do
        if [ -z "$sname" ]; then continue; fi
        path="$dir/$file"
        verdict="$(secret_set_verdict "$path")"
        case "$verdict" in
        readable)
            if grep -q '[^[:space:]]' "$path"; then
                secret_ready+="$sname"$'\t'"$scope"$'\t'"$file"$'\t'"$stores"$'\n'
            else
                secret_refused+="$sname"$'\t'"$file"$'\t'"$path is empty, an unfilled placeholder"$'\n'
            fi
            ;;
        missing*) secret_missing+="$sname"$'\t'"$scope"$'\t'"$file"$'\t'"$stores"$'\n' ;;
        *) secret_refused+="$sname"$'\t'"$file"$'\t'"${verdict#refused }"$'\n' ;;
        esac
    done <<<"$rows"
}

# Print the secrets no file could supply, and the ones whose file was refused, as two separate lists.
# They are separate because the fixes differ: a missing file is created and filled, and a refused file is repaired.
# It prints nothing for an empty list.
report_secret_problems() { # secrets-dir
    local sname scope file stores why
    if [ -n "$secret_missing" ]; then
        echo "Secrets with no file, so not set:"
        while IFS=$'\t' read -r sname scope file stores; do
            if [ -z "$sname" ]; then continue; fi
            echo "  $sname ($scope, stores: ${stores//,/ }) expects $1/$file"
        done <<<"$secret_missing"
    fi
    if [ -n "$secret_refused" ]; then
        echo "Secrets whose file was refused, so not set:"
        while IFS=$'\t' read -r sname file why; do
            if [ -z "$sname" ]; then continue; fi
            echo "  $sname: $why"
        done <<<"$secret_refused"
    fi
}

# Print what the secrets group would set, per store, without writing or printing a value, then the problems, and exit.
# The exit status is the one a real apply would end with, so a dry run that exits 0 predicts a run that does.
# Only the secrets group has a dry run, and the message says so rather than imply the other groups were previewed.
dry_run_secrets() { # secrets-dir
    local store sname scope file stores
    echo "Dry run for $repo. Nothing is written and no secret value is printed. Only the secrets group has a dry run, so the settings, labels, rulesets, project link, and agent policy files are not previewed."
    for store in actions dependabot; do
        echo "Secrets that would be set in the $store store from $1:"
        while IFS=$'\t' read -r sname scope file stores; do
            if [ -z "$sname" ]; then continue; fi
            case ",$stores," in
            *",$store,"*) echo "  $sname ($scope) from $file" ;;
            esac
        done <<<"$secret_ready"
    done
    report_secret_problems "$1"
    if [ -n "$secret_missing$secret_refused" ]; then
        exit 1
    fi
    exit 0
}

# ----- Ruleset id lookup (shared by apply and check) -----
# Map a ruleset name to the id of the first match, leaving it empty when nothing matches.
# It warns on duplicates, and aborts on an API error or at the per_page cap, where a single-fetch lookup is unreliable.
ruleset_id() {
    local out ids count
    # Requesting per_page=100 returns every ruleset in one array, since a repo carries only a handful.
    # The response is then a single JSON document, where a paginated fetch would concatenate arrays and break the jq below.
    # Let gh print its own error on stderr.
    # Add a context line and return non-zero, so the caller stops rather than treat an API failure as "not found".
    if ! out="$(gh api "repos/$repo/rulesets?per_page=100")"; then
        echo "Failed to list rulesets for $repo (check auth and repo access)." >&2
        return 1
    fi
    # A non-array 2xx body (truncated or proxy-mangled) must not reach the length guard or the ids= extraction below.
    # Depending on the shape, length or the .[] extraction below either errors or mis-reads it, inside this $(...) subshell where errexit does not propagate.
    # Both call sites would then misread the unreadable response as "not found" instead of aborting.
    if ! jq_has 'type == "array"' <<<"$out"; then
        echo "Failed for $repo: could not read live ruleset state (the response was not a JSON array)." >&2
        return 1
    fi
    # Fail loud rather than silently narrow, because a full page means the single-fetch assumption no longer holds.
    # A missed lookup would make apply create a duplicate ruleset by name.
    # Abort so the caller stops, since it treats a non-zero return as "stop" and never as "not found".
    if [ "$(jqr 'length' <<<"$out")" -eq 100 ]; then
        echo "Failed for $repo: 100 rulesets returned (the per_page cap), so the single-fetch lookup is unreliable. Reduce rulesets or add pagination before applying." >&2
        return 1
    fi
    # shellcheck disable=SC2016  # $n is a jq --arg variable, not a shell expansion
    if ! ids="$(jqr --arg n "$1" '.[] | if type == "object" then select(.name == $n) | if (.id | type) == "number" and .id == (.id | floor) then .id else error("bad id") end else error("not an object") end' <<<"$out")"; then
        echo "Failed for $repo: could not read live ruleset state (an array element was not a ruleset object, or a match had no integer id)." >&2
        return 1
    fi
    if [ -z "$ids" ]; then return 0; fi
    # Pre-existing drift can leave more than one ruleset with the same name.
    # Use the first and warn, so the duplicates get resolved rather than silently operating on the wrong one.
    # Both grep -c and sed read all their input with no early pipe close, so neither SIGPIPEs jq under pipefail.
    count="$(printf '%s\n' "$ids" | grep -c .)"
    if [ "$count" -gt 1 ]; then
        echo "Warning: $count rulesets named '$1' on $repo. Using the first (resolve the duplicates)." >&2
    fi
    printf '%s\n' "$ids" | sed -n '1p'
}

# ----- Fleet project lookup (shared by apply and check) -----
# Test with `project_payload_ok`, which is true only when project.json parses to all of these:
#   - an owner that is a non-empty string holding no tab or line break
#   - a title that is a non-empty string holding no tab or line break
#   - a number that is a positive integer
# The type tests keep a missing field from rendering as the literal string null in a message or a query variable.
# The number is tested as the text jq will print rather than as the value jq parsed, because jq preserves the source literal.
# 1.0 and 1e2 both equal their own floor, and both reach gh as 1.0 and 1E+2.
# The gh CLI sends those to the typed query variable as a string that the API then rejects.
# The nine-digit cap keeps a number GraphQL's own 32-bit Int cannot carry out of the query for the same reason.
project_payload_ok() {
    jq -e '(.owner|type=="string") and (.title|type=="string") and (.number|type=="number") and (.owner|length) > 0 and (.title|length) > 0 and ((.owner+.title)|test("[\t\r\n]")|not) and ((.number|tostring)|test("^[1-9][0-9]{0,8}$"))' "$project_file" >/dev/null 2>&1
}

# Print the node id of the project declared in project.json, or fail with a message naming what could not be resolved.
# The query goes through repositoryOwner rather than user or organization, since projectV2 is on the ProjectV2Owner interface both of those implement.
# The declared owner therefore needs no kind field for this script to guess wrong from.
# The declared title is asserted against the live one because a project number is unique only per owner and is reusable.
# A deleted and recreated project can leave the declared number pointing at a project the fleet never chose.
# The title is also asserted because project_node_id is the one place that resolves the number.
# It does so before every fleet repo is linked to what it names.
project_node_id() { # owner number title
    local out id title
    # -f sends a string and -F sends a typed value, so the login goes through -f.
    # An all-digit login under -F would reach a string query variable as a number and fail the query.
    # shellcheck disable=SC2016  # $owner and $number are GraphQL query variables, not shell expansions
    if ! out="$(gh api graphql -f query='query($owner: String!, $number: Int!) { repositoryOwner(login: $owner) { ... on ProjectV2Owner { projectV2(number: $number) { id title } } } }' -f owner="$1" -F number="$2")"; then
        # The call fails on a number that resolves to nothing as well as on a token that cannot see projects.
        # The gh call exits 1 on both, so the message names both rather than asserting the one it cannot tell apart.
        echo "Failed to read project number $2 owned by $1. Either no such project exists or the token cannot read projects (gh auth refresh -s project)." >&2
        return 1
    fi
    id="$(jqr '.data.repositoryOwner.projectV2.id // empty' <<<"$out")"
    if [ -z "$id" ]; then
        echo "No project number $2 owned by $1, which is what $project_file declares." >&2
        return 1
    fi
    title="$(jqr '.data.repositoryOwner.projectV2.title // empty' <<<"$out")"
    if [ "$title" != "$3" ]; then
        echo "Project number $2 owned by $1 is titled '$title', not the '$3' that $project_file declares. Refusing to act on a number pointing at another project." >&2
        return 1
    fi
    printf '%s\n' "$id"
}

# Print the target repository's own node id and the ids of the projects it is linked to, as one JSON object.
# One query answers both, because apply needs the repository id to write the link.
# It is also because both modes need the list to know whether the link is already there.
# A null repository is its own failure.
# The gh CLI exits non-zero on a GraphQL error.
# But a response carrying data and no such repository would otherwise read as a repository linked to nothing.
repo_projects() {
    local out
    # shellcheck disable=SC2016  # $owner and $name are GraphQL query variables, not shell expansions
    if ! out="$(gh api graphql -f query='query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id projectsV2(first: 100) { totalCount nodes { id } } } }' -f owner="${repo%%/*}" -f name="${repo##*/}")"; then
        echo "Failed to read the projects linked to $repo. Either the repository is unreadable or the token cannot read projects (gh auth refresh -s project)." >&2
        return 1
    fi
    if ! jq -e '.data.repository != null' <<<"$out" >/dev/null 2>&1; then
        echo "No repository $repo in the GraphQL response, so the projects linked to it could not be read." >&2
        return 1
    fi
    # Fail loud on a truncated list rather than silently narrow.
    # A page short of the whole set would read as the fleet link missing, and apply would then write a link that is already there.
    # The comparison is jq's rather than a bash integer test on a captured string.
    # Such a test would read false on any non-numeric output and let the truncated list through.
    if jq -e '.data.repository.projectsV2.totalCount > (.data.repository.projectsV2.nodes|length)' <<<"$out" >/dev/null 2>&1; then
        echo "Failed for $repo: more projects are linked than the single page read, so the lookup is unreliable. Add pagination before applying." >&2
        return 1
    fi
    jq -c '{ id: .data.repository.id, projects: [.data.repository.projectsV2.nodes[].id] }' <<<"$out"
}

# =============================== apply ===============================
apply_ruleset() { # payload-file - create-or-update the ruleset by name
    local file="$1" rname id live_bypass body
    if [ ! -e "$file" ]; then
        echo "Ruleset payload $file not found. Aborting to avoid a partially-applied configuration." >&2
        exit 1
    fi
    rname="$(jqr '.name // empty' "$file")"
    if [ -z "$rname" ]; then
        echo "Ruleset payload $file (composed from its source payload) has no name. Aborting to avoid a partially-applied configuration." >&2
        exit 1
    fi
    id="$(ruleset_id "$rname")"
    if [ -n "$id" ]; then
        echo "Updating ruleset '$rname' (id $id) on $repo"
        # The bypass list is a human decision, so this script neither grants nor revokes it.
        # A PUT replaces the whole document, so omitting the field would delete the live list rather than leave it alone.
        # The live value is therefore read and written back unchanged, which is what "hands off" has to mean against a replacing API.
        # A read failure aborts rather than proceeding, since applying without it would silently clear the list.
        if ! live_bypass="$(gh api "repos/$repo/rulesets/$id" --jq '.bypass_actors // []')"; then
            echo "Could not read the live bypass list for ruleset '$rname' on $repo. Aborting rather than applying a payload that would clear it." >&2
            exit 1
        fi
        if ! body="$(jq --argjson b "$live_bypass" '.bypass_actors = $b' "$file")"; then
            echo "Could not compose the ruleset payload for '$rname'. Aborting." >&2
            exit 1
        fi
        gh api --method PUT "repos/$repo/rulesets/$id" --input - <<<"$body" >/dev/null
    else
        echo "Creating ruleset '$rname' on $repo"
        # No bypass list is sent on create, so a new ruleset starts with GitHub's own empty default.
        # Nothing is deleted here, because nothing existed to delete.
        gh api --method POST "repos/$repo/rulesets" --input "$file" >/dev/null
    fi
}

# Test with `labels_payload_ok`, which succeeds only when labels.json parses to an array with all of these properties:
#   - it is non-empty
#   - its labels are unique by name ignoring case
#   - each label meets the API's field contract
# That field contract is:
#   - a non-empty name
#   - a six-digit lowercase hex color, which is how GitHub stores it
#   - a description of at most 100 characters
# Each is a string holding no tab or line break.
# Each step runs its own jq and prints its own reason on a failure.
# A payload that is malformed, empty, out of contract, or repeats a name is therefore told apart from a jq that failed.
labels_payload_ok() {
    local err status step
    local -a steps=(
        'length == 1@@does not hold exactly one JSON document'
        '.[0] | type=="array"@@is not an array'
        '.[0] | length > 0@@is empty'
        '.[0] | all(.[]; type=="object" and (.name|type=="string") and (.color|type=="string") and (.description|type=="string") and (.name|length) > 0 and (.color|test("^[0-9a-f]{6}$")) and (.description|length) <= 100 and ((.name+.color+.description)|test("[\t\r\n]")|not))@@holds a label outside the field contract (non-empty name, six-digit lowercase hex color, description of at most 100 characters, no tab or line break)'
        '.[0] | (map(.name | ascii_downcase) | unique | length) == length@@names a label more than once, ignoring case'
    )
    status=0
    err="$(jq empty "$labels_file" 2>&1)" || status=$?
    if [ "$status" -ne 0 ] && [[ "$err" == *"parse error"* ]]; then
        echo "did not parse (${err//$'\n'/ })"
        return 1
    elif [ "$status" -ne 0 ]; then
        echo "could not be checked because jq failed (exit $status: ${err//$'\n'/ })"
        return 1
    fi
    for step in "${steps[@]}"; do
        status=0
        err="$(jq -s -e "${step%%@@*}" "$labels_file" 2>&1 >/dev/null)" || status=$?
        if [ "$status" -eq 1 ]; then
            echo "${step#*@@}"
            return 1
        elif [ "$status" -ne 0 ]; then
            echo "could not be checked because jq failed (exit $status: ${err//$'\n'/ })"
            return 1
        fi
    done
}

apply_labels() { # create-or-update every label labels.json declares, by name
    # `gh label create --force` updates a label that exists and creates one that does not, so the write is idempotent by name.
    # A label the payload does not declare is left standing, because a repo may carry labels of its own and this script deletes nothing.
    # Every field is read from the payload, so a label added there reaches every fleet repo on the next apply with no change here.
    # The payload is parsed into a variable before the loop for the reason check_settings gives: a jq failure inside `done < <(...)` skips the body silently.
    local rows lname color desc
    # The payload was validated by cmd_apply's pre-flight, before any write, so this read cannot be the first to find it malformed.
    rows="$(jqr '.[] | "\(.name)\t\(.color)\t\(.description)"' "$labels_file")"
    while IFS=$'\t' read -r lname color desc; do
        gh label create "$lname" --repo "$repo" --color "$color" --description "$desc" --force >/dev/null
    done <<<"$rows"
    echo "Applied $(wc -l <<<"$rows" | tr -d ' ') labels from labels.json"
}

# Link the repository to the fleet project, which is an association between two objects rather than a repository setting.
# It is therefore written through its own API and depends on none of the writes above.
# The live link list is read first and the mutation runs only when the declared project is absent from it.
# A second apply is therefore a read rather than a repeated write, and no write is ever fired to discover whether one was needed.
# The project's node id is resolved by cmd_apply's pre-flight and passed in.
# That is because resolving it here would abort the run after the four earlier groups had already been written.
apply_project() { # project-node-id
    local pid="$1" owner number title live rid out
    owner="$(jqr '.owner' "$project_file")"
    number="$(jqr '.number' "$project_file")"
    title="$(jqr '.title' "$project_file")"
    live="$(repo_projects)"
    if jq -e --arg p "$pid" '.projects | index($p) != null' <<<"$live" >/dev/null 2>&1; then
        echo "Already linked to project '$title' ($owner, number $number)"
        return
    fi
    rid="$(jqr '.id' <<<"$live")"
    # The call is guarded rather than left to abort on its own.
    # That is because a bare command substitution ends the run with gh's error and nothing of this script's.
    # The failure it was written for is a token that reads projects and cannot write a link.
    # The pre-flight's resolution cannot catch it, because that one is a read.
    # Every other non-zero exit lands here too.
    # The response is then read rather than discarded, and the repository it names is asserted against the one just linked.
    # A write that returned anything else therefore stops the run instead of reporting a link that may not exist.
    # shellcheck disable=SC2016  # $project and $repository are GraphQL query variables, not shell expansions
    if ! out="$(gh api graphql -f query='mutation($project: ID!, $repository: ID!) { linkProjectV2ToRepository(input: { projectId: $project, repositoryId: $repository }) { repository { id } } }' -f project="$pid" -f repository="$rid")"; then
        echo "Failed to link $repo to project '$title' ($owner, number $number). The token may read projects and still not be able to write a link (gh auth refresh -s project)." >&2
        exit 1
    fi
    if [ "$(jqr '.data.linkProjectV2ToRepository.repository.id // empty' <<<"$out")" != "$rid" ]; then
        echo "Linking $repo to project '$title' ($owner, number $number) returned no matching repository, so the link is unconfirmed: $out" >&2
        exit 1
    fi
    echo "Linked $repo to project '$title' ($owner, number $number)"
}

# Set every ready secret in every store that needs it, then report what could not be set and fail if anything could not.
# The plan was computed by cmd_apply's pre-flight, so a stray or an unreadable tree never reaches here.
# Each value goes to gh on stdin from its file and is never in an argument or echoed.
# The gh CLI strips trailing carriage returns and line feeds from a value read on stdin, so the file is passed as it is.
# Every name whose file is present is written on every run, whatever the live store holds, since a value is never readable to compare against.
# The writes run unguarded under `set -e`, so a failed write stops the run with gh's own error and nothing suppressed.
# This function is never called in a condition, which would switch errexit off inside it.
# It therefore ends the run itself when something is missing or refused.
apply_secrets() { # secrets-dir
    local dir="$1" sname scope file stores store
    local -a store_list
    while IFS=$'\t' read -r sname scope file stores; do
        if [ -z "$sname" ]; then continue; fi
        IFS=, read -r -a store_list <<<"$stores"
        for store in "${store_list[@]}"; do
            # The file is the write's stdin, which also keeps gh from reading the remaining rows of this loop.
            gh secret set "$sname" --repo "$repo" --app "$store" <"$dir/$file"
            echo "Set $sname ($scope) in the $store store of $repo"
        done
    done <<<"$secret_ready"
    report_secret_problems "$dir"
    if [ -n "$secret_missing$secret_refused" ]; then
        echo "Configuration applied to $repo except for the secrets listed above, which were not set." >&2
        exit 1
    fi
}

cmd_apply() {
    local f private disc payload project_id why
    # Pre-flight every required payload before any write, so a partial carry aborts before it half-applies.
    for f in "$settings_file" "$labels_file" "$project_file" "$develop_ruleset" "$main_ruleset"; do
        if [ ! -e "$f" ]; then
            echo "Required payload $f not found. Aborting to avoid a partially-applied configuration." >&2
            exit 1
        fi
    done
    # The agent policy files are planned here too, because a checkout that cannot take them would otherwise fail after every API write.
    if [ "$checkout_given" = true ]; then
        if ! command -v uv >/dev/null 2>&1; then
            echo "uv not found. Writing the agent policy files runs $agent_policy under uv. Aborting before any write." >&2
            exit 1
        fi
        if ! uv run --no-project --script "$agent_policy" write "${agent_policy_args[@]}" --repo "$repo" --checkout "$checkout" --dry-run; then
            echo "The agent policy files cannot be written into $checkout. Aborting before any write." >&2
            exit 1
        fi
    fi
    # The label payload's content is validated here too, since apply_labels runs after the settings and Dependabot writes and an abort there would leave them applied.
    if ! why="$(labels_payload_ok)"; then
        echo "Label payload $labels_file $why. Aborting before any write." >&2
        exit 1
    fi
    # The project payload is validated here for the same reason.
    # That is because apply_project runs last of all and an abort inside it would leave every write before it applied.
    if ! project_payload_ok; then
        echo "Project payload $project_file did not parse or declares a field outside the contract (a non-empty owner and title holding no tab or line break, and a number written as at most nine plain digits). Aborting before any write." >&2
        exit 1
    fi
    # The project is resolved here rather than in apply_project.
    # That is because it is a read.
    # In apply_project, a number that resolves to nothing, or to a project titled otherwise, would abort the run with four groups of writes already applied.
    project_id="$(project_node_id "$(jqr '.owner' "$project_file")" "$(jqr '.number' "$project_file")" "$(jqr '.title' "$project_file")")"
    # The secret plan is computed here too, since the tree and the files it reads can fail after every API write otherwise.
    # A stray exits inside plan_secrets, and a dry run exits inside dry_run_secrets, so neither reaches a write.
    if [ "$secrets_registered" = true ]; then
        if ! load_secret_names; then
            echo "Aborting before any write." >&2
            exit 1
        fi
        plan_secrets "${HOME:?}/.secrets"
        if [ "$dry_run" = true ]; then
            dry_run_secrets "$HOME/.secrets"
        fi
    elif [ "$dry_run" = true ]; then
        echo "Dry run for $repo. It has no registry entry in $registry, so no secret names are declared and there is nothing to preview."
        exit 0
    fi
    echo "Applying configuration to $repo (model: $model)"
    # The writes below silence stdout only, because the success-response JSON is noise.
    # They still fail loud, since gh errors go to stderr and a failed write aborts the script.
    # These writes run unguarded under `set -e`.
    # ----- General repository settings -----
    # Discussions are enabled on public repos only by fleet policy, and never on a private one.
    private="$(gh api "repos/$repo" --jq '.private')"
    disc=false
    [ "$private" = "false" ] && disc=true
    # The default branch is main, but only point it there once main exists.
    # Never set the default to a missing branch, as on a repo still living on a rework branch.
    if gh api "repos/$repo/branches/main" --jq '.name' >/dev/null 2>&1; then
        payload="$(jq --argjson d "$disc" '. + {has_discussions: $d, default_branch: "main"}' "$settings_file")"
    else
        payload="$(jq --argjson d "$disc" '. + {has_discussions: $d}' "$settings_file")"
        echo "Warning: $repo has no 'main' branch. Leaving default_branch unchanged." >&2
    fi
    # Applies only once a repo declares the field (see the resolution above).
    if [ -n "$description" ]; then
        payload="$(jq --arg desc "$description" '. + {description: $desc}' <<<"$payload")"
    fi
    echo "Applying general settings (has_discussions=$disc$([ -n "$description" ] && echo ", description from registry/repos.json"))"
    printf '%s' "$payload" | gh api --method PATCH "repos/$repo" --input - >/dev/null
    # ----- Dependabot alerts + automated security updates -----
    gh api --method PUT "repos/$repo/vulnerability-alerts" >/dev/null
    gh api --method PUT "repos/$repo/automated-security-fixes" >/dev/null
    echo "Enabled Dependabot vulnerability alerts + automated security updates"
    # ----- Fleet label set -----
    apply_labels
    # ----- Branch rulesets (main shared, develop selected by workflow model) -----
    apply_ruleset "$develop_ruleset"
    apply_ruleset "$main_ruleset"
    # ----- Fleet project link -----
    apply_project "$project_id"
    # ----- Agent policy files (only into a checkout the caller names) -----
    if [ "$checkout_given" = true ]; then
        echo "Writing the agent policy files into $checkout (not staged or committed)"
        uv run --no-project --script "$agent_policy" write "${agent_policy_args[@]}" --repo "$repo" --checkout "$checkout"
    else
        echo "Note: the agent policy files were not written. Run '$0 apply${repo_arg:+ $repo} --checkout <path>' to write them into a checkout, then commit them through a pull request."
    fi
    # ----- Secrets, read from the host's ~/.secrets/ and written last -----
    if [ "$secrets_registered" = true ]; then
        apply_secrets "$HOME/.secrets"
    else
        echo "Note: $repo has no registry entry in $registry, so no secret names are declared and none were set."
    fi
    echo "Configuration applied to $repo. Run '$0 check${repo_arg:+ $repo}' to validate."
}

# =============================== check ===============================
FAILED=0
note() { printf '  %s\n' "$*"; }
pass() { printf '  ok   %s\n' "$*"; }
fail() {
    printf '  FAIL %s\n' "$*"
    FAILED=1
}

# Run a test command with `assert MESSAGE TEST...`, passing on success and failing on non-zero.
# It is a proper if/else rather than the `A && B || C` footgun.
# Do not redirect the assert call's own stdout, which would swallow the pass or fail line.
# A command that prints, such as jq, goes through jq_has, which silences only itself.
assert() {
    local msg="$1"
    shift
    if "$@"; then pass "$msg"; else fail "$msg"; fi
}

# Test with `jq_has FILTER...`, which is true only when the filter selects a truthy value.
# The jq output is discarded, not the caller's.
# It reads its JSON from stdin.
jq_has() { jq -e "$@" >/dev/null 2>&1; }

# Test with `gh_ok ENDPOINT...`, which is true only when the gh api call succeeds, a 204 included.
# Output and errors are both discarded, so it is safe to pass to assert.
# The vulnerability-alerts endpoint is the example, returning 204 when enabled and 404 when disabled.
gh_ok() { gh api "$@" >/dev/null 2>&1; }

# Call as `jqr FILTER...`: jq -r plus stripping a line's trailing \r that a native Windows jq (WinGet's jq-1.8.2) puts there.
# The \r bash expands here, a literal byte rather than sed's own backslash-escape.
# That is because sed's own escape is a GNU extension a BSD sed would not honor.
jqr() { jq -r "$@" | sed $'s/\r$//'; }

check_ruleset() { # payload-file - the live ruleset must match the committed policy, driven by the payload
    local file="$1" rname id live t want got want_enf
    if [ ! -e "$file" ]; then
        fail "ruleset payload $file missing"
        return
    fi
    if ! rname="$(jqr '.name // empty' "$file")"; then
        fail "ruleset payload $file did not parse"
        return
    fi
    if [ -z "$rname" ]; then
        fail "ruleset payload $file has no name"
        return
    fi
    if ! id="$(ruleset_id "$rname")"; then
        fail "ruleset '$rname' - could not resolve id"
        return
    fi
    if [ -z "$id" ]; then
        fail "ruleset '$rname' missing"
        return
    fi
    if ! live="$(gh api "repos/$repo/rulesets/$id")"; then
        fail "ruleset '$rname' - could not read live state"
        return
    fi
    if ! want_enf="$(jqr '.enforcement' "$file")"; then
        fail "ruleset payload $file did not parse"
        return
    fi
    assert "ruleset '$rname' enforcement = $want_enf" test "$(jqr '.enforcement' <<<"$live")" = "$want_enf"
    # The live rule-type set must equal the payload's, compared in both directions.
    # Checking only that each payload type is present live misses a rule someone added by hand.
    # That is drift this script exists to catch, and it passed as clean before.
    local want_types got_types
    if ! want_types="$(jqr '[.rules[].type] | sort | join(",")' "$file")"; then
        fail "ruleset payload $file did not parse"
        return
    fi
    if [ -z "$want_types" ]; then
        fail "ruleset payload $file declares no rules"
        return
    fi
    if ! got_types="$(jqr '[.rules[].type] | sort | join(",")' <<<"$live")"; then
        fail "live ruleset for '$rname' did not parse"
        return
    fi
    assert "'$rname' rule set = $want_types" test "$got_types" = "$want_types"
    # The bypass list is reported and never asserted, because no payload declares one.
    # Who may bypass a ruleset is a human decision taken in the UI, so code states what is there and judges nothing.
    # It is surfaced on every run rather than left invisible, since it is the field that decides who the rules do not apply to.
    local bypass
    bypass="$(jqr '[.bypass_actors[]? | "\(.actor_type) \(.actor_id) \(.bypass_mode)"] | join("; ")' <<<"$live")"
    note "ruleset '$rname' bypass list: ${bypass:-none} (not managed by this script)"
    # Every parameterized rule is compared on its whole parameters object rather than on selected fields.
    # Naming fields one at a time meant a payload could declare a parameter the check never read.
    # Review-thread resolution, stale-review dismissal, and the status-check policy flags all went unverified that way.
    # Comparing the object keeps the check payload-driven: a parameter added to a payload is audited with no code change.
    # Keys are sorted on both sides, so key order from the API cannot read as drift.
    # Set-like arrays are sorted too, since the API guarantees no order and the previous per-field comparison sorted them explicitly.
    # Dropping that would turn array order into false drift.
    # A scalar array sorts directly, and required_status_checks sorts by context, its identifying field.
    local ptypes norm
    # The walk/1 builtin arrived in jq 1.6, which the declared floor makes available.
    # It is defined here rather than called so the filter also compiles below the floor.
    # A host on jq 1.5 would otherwise not degrade, it would fail to compile the filter.
    # The check_ruleset function would then report drift on every parameterized rule it never actually compared.
    # That is the inverse of the false clean this comparison was written to close, so the portable definition is worth its length.
    # The keys_unsorted builtin the definition calls needs no such stand-in, since jq 1.5 evaluates it where walk(.) fails to compile.
    # shellcheck disable=SC2016  # $in and $k are jq's variables, not shell expansions
    norm='def w(f): . as $in
            | if type == "object" then reduce keys_unsorted[] as $k ({}; . + { ($k): ($in[$k] | w(f)) }) | f
              elif type == "array" then map(w(f)) | f
              else f end;
          def n: w(if type=="array" then (if length==0 then . elif (all(.[]; type=="string" or type=="number")) then sort elif (all(.[]; type=="object" and has("context"))) then sort_by(.context) else . end) else . end); n'
    ptypes="$(jqr '[.rules[] | select(has("parameters")) | .type] | .[]' "$file")"
    while IFS= read -r t; do
        [ -z "$t" ] && continue
        want="$(jq -S -c --arg t "$t" "[.rules[] | select(.type==\$t) | .parameters] | first | $norm" "$file")"
        got="$(jq -S -c --arg t "$t" "[.rules[] | select(.type==\$t) | .parameters] | first | $norm" <<<"$live")"
        assert "'$rname' rule '$t' parameters match the payload" test "$got" = "$want"
    done <<<"$ptypes"
}

check_settings() {
    local live key want got private wantdisc
    if [ ! -e "$settings_file" ]; then
        fail "settings payload $settings_file missing"
        return
    fi
    if ! live="$(gh api "repos/$repo")"; then
        fail "could not read repository settings"
        return
    fi
    if ! jq_has -s 'length == 1 and (.[0] | type == "object")' <<<"$live"; then
        fail "live repository settings for '$repo' were not one JSON object"
        return
    fi
    # Static settings are driven from settings.json, so the check never drifts from the file.
    # Add a key there and it is audited here automatically.
    # The payload is parsed into a variable before the loop rather than streamed from a process substitution.
    # That is because a jq failure inside `done < <(...)` would leave the loop body unexecuted without tripping set -e.
    # It would report every setting as checked and passing while nothing was compared, a false clean.
    local pairs
    if ! pairs="$(jqr 'to_entries[] | "\(.key)\t\(.value)"' "$settings_file")"; then
        fail "settings payload $settings_file did not parse"
        return
    fi
    # A payload that parses to nothing is a floor failure rather than a clean run, so it is asserted.
    if [ -z "$pairs" ]; then
        fail "settings payload $settings_file declares no keys"
        return
    fi
    while IFS=$'\t' read -r key want; do
        # shellcheck disable=SC2016  # $k is a jq --arg variable, not a shell expansion
        got="$(jqr --arg k "$key" '.[$k]' <<<"$live")"
        assert "setting $key = $want" test "$got" = "$want"
    done <<<"$pairs"
    # Dynamic settings apply sets: has_discussions (public repos only), default_branch (main, if it exists).
    private="$(jqr '.private' <<<"$live")"
    wantdisc=true
    [ "$private" = "true" ] && wantdisc=false
    assert "has_discussions = $wantdisc" test "$(jqr '.has_discussions' <<<"$live")" = "$wantdisc"
    if gh api "repos/$repo/branches/main" --jq '.name' >/dev/null 2>&1; then
        assert "default_branch = main" test "$(jqr '.default_branch' <<<"$live")" = main
    fi
    # $description is empty for two different reasons, told apart below since each needs different follow-up.
    if [ -n "$description" ]; then
        assert "description = '$description'" test "$(jqr '.description' <<<"$live")" = "$description"
    elif [ ! -f "$registry" ]; then
        note "description: no $registry to read (it resolves relative to this script, not from the repo argument). Run from a hub checkout for it to exist, and verify manually."
    else
        note "description: no matching registry entry or no declared description key for $name (falls back to the README tagline, see GOVERNANCE.md 'Repository Details'). Verify manually."
    fi
}

check_security() {
    local sec
    # The vulnerability-alerts endpoint returns 204 when enabled and 404 when disabled, so probe it with gh_ok.
    # The automated-security-fixes endpoint returns a JSON body of { enabled, paused }.
    # That one is captured under an explicit failure guard, so a read error is a clean FAIL rather than a set -e abort.
    assert "Dependabot vulnerability alerts enabled" gh_ok "repos/$repo/vulnerability-alerts"
    if sec="$(gh api "repos/$repo/automated-security-fixes" 2>/dev/null)"; then
        assert "Dependabot automated security updates enabled" jq_has '.enabled == true' <<<"$sec"
    else
        fail "Dependabot automated security updates - could not read the setting"
    fi
}

check_labels() {
    local live rows lname color desc got extra why
    if [ ! -e "$labels_file" ]; then
        fail "label payload $labels_file missing"
        return
    fi
    # The list is paginated, since a repo carrying more labels than one page holds would otherwise report a later-page label as missing.
    if ! live="$(gh api --paginate "repos/$repo/labels" --jq '.[]' | jq -s '.')"; then
        fail "could not read repository labels"
        return
    fi
    if ! why="$(labels_payload_ok)"; then
        fail "label payload $labels_file $why"
        return
    fi
    rows="$(jqr '.[] | "\(.name)\t\(.color)\t\(.description)"' "$labels_file")"
    # Each declared label is asserted on all three fields, so a color or description edited by hand reads as drift.
    while IFS=$'\t' read -r lname color desc; do
        # shellcheck disable=SC2016  # $n is a jq --arg variable, not a shell expansion
        got="$(jqr --arg n "$lname" '[.[] | select(.name == $n)] | first // empty | "\(.color)\t\(.description // "")"' <<<"$live")"
        assert "label '$lname' = $color '$desc'" test "$got" = "$color"$'\t'"$desc"
    done <<<"$rows"
    # A label the payload never declared is reported rather than asserted, matching the bypass list.
    # The fleet set is a floor, and a repo may add its own.
    # shellcheck disable=SC2016  # $want is a jq --slurpfile variable, not a shell expansion
    if ! extra="$(jqr --slurpfile want "$labels_file" '[.[].name] - [$want[0][].name] | join(", ")' <<<"$live")"; then
        fail "could not compute the undeclared label list"
        return
    fi
    note "labels not declared by labels.json: ${extra:-none} (left alone by this script)"
}

check_project() {
    local owner number title pid live extra
    if [ ! -e "$project_file" ]; then
        fail "project payload $project_file missing"
        return
    fi
    if ! project_payload_ok; then
        fail "project payload $project_file did not parse or declares a field outside the contract (a non-empty owner and title holding no tab or line break, and a number written as at most nine plain digits)"
        return
    fi
    owner="$(jqr '.owner' "$project_file")"
    number="$(jqr '.number' "$project_file")"
    title="$(jqr '.title' "$project_file")"
    # Each read reports its own failure and returns.
    # An unreadable project is therefore a FAIL for this group rather than an abort leaving the groups after it unchecked.
    if ! pid="$(project_node_id "$owner" "$number" "$title")"; then
        fail "project '$title' ($owner, number $number) - could not resolve the declared project"
        return
    fi
    if ! live="$(repo_projects)"; then
        fail "project '$title' ($owner, number $number) - could not read the projects linked to $repo"
        return
    fi
    # shellcheck disable=SC2016  # $p is a jq --arg variable, not a shell expansion
    assert "linked to project '$title' ($owner, number $number)" jq_has --arg p "$pid" '.projects | index($p) != null' <<<"$live"
    # A project the payload never declared is reported rather than asserted, matching the label group.
    # The fleet link is a floor, and a repo may be linked to a project of its own.
    # shellcheck disable=SC2016  # $p is a jq --arg variable, not a shell expansion
    if ! extra="$(jqr --arg p "$pid" '[.projects[] | select(. != $p)] | length' <<<"$live")"; then
        fail "project '$title' ($owner, number $number) - could not count the repository's other project links"
        return
    fi
    note "projects linked beyond the declared one: $extra (left alone by this script)"
}

check_environments() {
    local entries entry_count count i row ename ename_uri policy live_envs env_live got want policies extra
    if [ ! -f "$registry" ]; then
        note "deployment environments: no $registry to read (it resolves relative to this script, not from the repo argument). Run from a hub checkout for it to exist, and verify manually."
        return
    fi
    # Registration is answered by its own read rather than by a sentinel value folded into the next one.
    # No JSON scalar is free to serve as that sentinel.
    # A declared null prints exactly as one would.
    # An entry declaring an invalid null would therefore report as an unregistered repo and skip the shape test below that exists to catch it.
    # Fail rather than default on a read error, matching the model lookup.
    # A registry that will not parse is a broken run, not a repo with no environments.
    # shellcheck disable=SC2016  # $n is a jq --arg variable, not a shell expansion
    if ! entry_count="$(jqr --arg n "$name" '[.repos[] | select(.name == $n)] | length' "$registry")"; then
        fail "could not read $registry to resolve the entry for $name"
        return
    fi
    # Only the zero case is reachable here.
    # A count above one raises in spec/resolve_description.py and exits the script above, before any check runs, so a branch for it here could never fire.
    if [ "$entry_count" != 1 ]; then
        note "deployment environments: no registry entry named '$name', so nothing is declared to check. Verify by hand whether this repo uses one."
        return
    fi
    # The declared value is emitted verbatim, invalid shapes included, so each one reaches the test that judges it rather than being defaulted away here.
    if ! entries="$(jq -c --arg n "$name" '[.repos[] | select(.name == $n)][0] | if has("environments") then .environments else [] end' "$registry")"; then
        fail "could not read the declared deployment environments from $registry"
        return
    fi
    # One list read serves both the per-environment assertions and the undeclared-environment note, and it is the call that surfaces an API or permission failure.
    # Reading each environment individually instead would let a 404 and a 403 arrive as the same result.
    # The 404 is for a missing environment and the 403 for a token without admin.
    if ! live_envs="$(gh api --paginate "repos/$repo/environments" --jq '.environments[]' | jq -s '.')"; then
        echo "Failed to list environments for $repo (check auth and repo access)." >&2
        fail "could not list the deployment environments on $repo"
        return
    fi
    # The authority on this shape is spec/validate.py, but this script runs against whatever hub checkout the operator has.
    # Every read below is therefore preceded by one test rather than left bare.
    # A bare read of a malformed entry aborts the whole run under set -e, mid-check.
    # That happens after the five groups above have printed their pass lines and before cmd_check reaches its drift summary.
    # This test is narrower than spec/validate.py's, which refuses shapes this one admits.
    # A registry that never ran the validator can therefore still report drift rather than the malformation causing it.
    if ! jq_has 'type == "array"' <<<"$entries"; then
        fail "registry environments for $name is not a list"
        return
    fi
    count="$(jqr 'length' <<<"$entries")"
    for ((i = 0; i < count; i++)); do
        row="$(jq -c ".[$i]" <<<"$entries")"
        # The branches test is by element type, not just by container.
        # A non-string element renders through string interpolation without erroring.
        # So [1, 2] would otherwise be asserted against the live set as the branch names "1" and "2".
        if ! jq_has '
            type == "object"
            and (.name | type == "string" and length > 0)
            and (.branchPolicy | . == "custom" or . == "none")
            and (if .branchPolicy == "custom"
                 then (.branches | type == "array" and all(type == "string" and length > 0))
                 else (has("branches") | not) end)' <<<"$row"; then
            fail "registry environments[$i] for $name is malformed (expected a name, a branchPolicy of custom or none, and branches of non-empty strings under custom only)"
            continue
        fi
        ename="$(jqr '.name' <<<"$row")"
        policy="$(jqr '.branchPolicy' <<<"$row")"
        env_live="$(jq -c --arg n "$ename" '[.[] | select(.name == $n)] | first // empty' <<<"$live_envs")"
        if [ -z "$env_live" ]; then
            fail "environment '$ename' missing"
            continue
        fi
        # Read the live shape as the one token the registry declares, rather than asserting two booleans a reader then has to recombine into a policy.
        # "protected" is derived but not declarable, so a live environment set to protected-branches-only always fails here.
        # That is the intent: it counts classic branch protection and the fleet configures rulesets instead.
        # "unrecognized" is deliberate for the same reason.
        # A form GitHub adds later therefore fails loudly instead of being folded into one of the three above it.
        got="$(jqr 'if .deployment_branch_policy == null then "none" elif .deployment_branch_policy.custom_branch_policies then "custom" elif .deployment_branch_policy.protected_branches then "protected" else "unrecognized" end' <<<"$env_live")"
        assert "environment '$ename' branch policy = $policy" test "$got" = "$policy"
        # The allowed set exists only under a custom policy, so a mismatch above skips it rather than reporting a second failure for the same cause.
        if [ "$policy" != custom ] || [ "$got" != custom ]; then continue; fi
        # GitHub documents no character restriction on an environment name beyond 255 characters and uniqueness, so a name legitimately holds a space, a '#', a '?' or a '/'.
        # Raw interpolation would turn each of those into something other than one path segment, and a '/' into two.
        # shellcheck disable=SC2016  # $s is a jq --arg variable, not a shell expansion
        ename_uri="$(jqr -n --arg s "$ename" '$s|@uri')"
        if ! policies="$(gh api --paginate "repos/$repo/environments/$ename_uri/deployment-branch-policies" --jq '.branch_policies[]' | jq -s '.')"; then
            fail "environment '$ename' - could not read its deployment branch policies"
            continue
        fi
        # A tag policy shares this endpoint with a branch policy, so the type is compared rather than filtered out.
        # A tag added by hand therefore reads as drift instead of vanishing from the set.
        # Both sides are sorted, since the endpoint's order is not the registry's.
        want="$(jqr '[.branches[] | "branch:\(.)"] | sort | join(", ")' <<<"$row")"
        got="$(jqr '[.[] | "\(.type // "branch"):\(.name)"] | sort | join(", ")' <<<"$policies")"
        assert "environment '$ename' allows exactly [$want]" test "$got" = "$want"
    done
    # An environment the registry does not declare is reported rather than asserted, matching the label list and the bypass actors.
    # The fleet set is a floor.
    # GitHub creates some without being asked, a Copilot coding-agent environment among them.
    # Asserting the set both ways would therefore report drift on every repo that has ever run one.
    # shellcheck disable=SC2016  # $live is a jq --argjson variable, not a shell expansion
    # Names are taken only from entries shaped like one.
    # That is because a malformed entry the loop already refused would otherwise raise here.
    # The raise would replace this note with a second failure naming the wrong cause.
    if ! extra="$(jqr --argjson live "$live_envs" '([$live[].name] - [.[] | objects | .name | strings]) | join(", ")' <<<"$entries")"; then
        fail "could not compute the undeclared deployment environment list"
        return
    fi
    note "deployment environments not declared by the registry: ${extra:-none} (reported, not asserted)"
}

check_agent_policy() { # the repo's default branch must carry the files the registry renders
    local branch
    if ! command -v uv >/dev/null 2>&1; then
        fail "uv not found, so the agent policy files were not checked ($agent_policy runs under uv)"
        return
    fi
    if ! branch="$(gh api "repos/$repo" --jq '.default_branch')"; then
        fail "could not read the default branch of $repo, so the agent policy files were not checked"
        return
    fi
    # Exit 1 is drift or a missing file and exit 2 a file that could not be read, and either fails the run.
    if ! uv run --no-project --script "$agent_policy" check "${agent_policy_args[@]}" --repo "$repo" --ref "$branch"; then
        FAILED=1
    fi
}

check_secrets() {
    local store live sname claimed forbidden wanted
    if [ "$secrets_registered" != true ]; then
        note "secrets: no registry entry named '$name', so no secret names are declared to check."
        return
    fi
    if ! load_secret_names; then
        fail "secrets - could not resolve the secret names $repo owes"
        return
    fi
    # Each list is read into a variable before its loop for the reason check_settings gives: a jq failure inside `done < <(...)` skips the body silently.
    claimed="$(jqr '.rows[].name' <<<"$secret_names_json")"
    forbidden="$(jqr '.forbidden[]' <<<"$secret_names_json")"
    for store in actions dependabot; do
        # The list is paginated, since a store holding more names than one page would otherwise report a later one as missing.
        if ! live="$(gh api --paginate "repos/$repo/$store/secrets" --jq '.secrets[].name')"; then
            fail "secrets - could not list the $store store"
            continue
        fi
        # shellcheck disable=SC2016  # $s is a jq --arg variable, not a shell expansion
        wanted="$(jqr --arg s "$store" '.rows[] | select(.stores | index($s)) | .name' <<<"$secret_names_json")"
        while IFS= read -r sname; do
            if [ -z "$sname" ]; then continue; fi
            if grep -qxF -- "$sname" <<<"$live"; then
                pass "secret $sname is in the $store store"
            else
                fail "secret $sname is missing from the $store store"
            fi
        done <<<"$wanted"
        while IFS= read -r sname; do
            if [ -z "$sname" ]; then continue; fi
            if grep -qxF -- "$sname" <<<"$live"; then
                fail "forbidden secret $sname is present in the $store store"
            fi
        done <<<"$forbidden"
        # A name nothing claims is reported rather than asserted, and never deleted.
        while IFS= read -r sname; do
            if [ -z "$sname" ]; then continue; fi
            if ! grep -qxF -- "$sname" <<<"$claimed" && ! grep -qxF -- "$sname" <<<"$forbidden"; then
                note "secret $sname in the $store store is claimed by no applicable mechanism (reported, never deleted)"
            fi
        done <<<"$live"
    done
    note "secret values are never readable via the API, so verify by hand that a value is valid"
}

cmd_check() {
    echo "Validating configuration for $repo (model: $model)"
    check_ruleset "$develop_ruleset"
    check_ruleset "$main_ruleset"
    check_settings
    check_security
    check_labels
    check_project
    check_environments
    check_agent_policy
    check_secrets
    if [ "$FAILED" -ne 0 ]; then
        echo "Configuration drift detected on $repo."
        exit 1
    fi
    echo "Configuration matches on $repo."
}

case "$cmd" in
apply) cmd_apply ;;
check) cmd_check ;;
esac

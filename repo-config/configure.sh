#!/usr/bin/env bash
# Apply the committed fleet configuration in this directory to the repository via the GitHub API:
#   1. General repository settings from settings.json (PATCH /repos), plus the two settings that depend on
#      per-repo state - has_discussions (public repos only) and default_branch (main, only if it exists).
#   2. The branch rulesets develop.json and main.json. Each <name>.json holds the writable ruleset subset
#      {name, target, enforcement, bypass_actors, conditions, rules}. An existing ruleset (matched by name) is
#      updated with a full-payload PUT (partial PUTs 422); a missing one is created with POST.
# Rerunning is idempotent.
#
# Usage: repo-config/configure.sh [owner/repo]   (defaults to the current repo via gh)
set -euo pipefail

repo="${1:-$(gh repo view --json nameWithOwner --jq '.nameWithOwner')}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----- General repository settings -----
settings_file="$script_dir/settings.json"
if [ -e "$settings_file" ]; then
    # has_discussions: enabled on public repos only (fleet policy); never on private.
    private="$(gh api "repos/$repo" --jq '.private')"
    disc=false; [ "$private" = "false" ] && disc=true
    # default_branch main, but only point it at main when main exists - never set the default to a missing
    # branch (e.g. a repo still on a rework branch).
    if gh api "repos/$repo/branches/main" --jq '.name' >/dev/null 2>&1; then
        payload="$(jq --argjson d "$disc" '. + {has_discussions: $d, default_branch: "main"}' "$settings_file")"
    else
        payload="$(jq --argjson d "$disc" '. + {has_discussions: $d}' "$settings_file")"
        echo "Warning: $repo has no 'main' branch; leaving default_branch unchanged." >&2
    fi
    echo "Applying general settings to $repo (has_discussions=$disc)"
    printf '%s' "$payload" | gh api --method PATCH "repos/$repo" --input - >/dev/null
fi

# ----- Branch rulesets -----
for file in "$script_dir"/*.json; do
    [ -e "$file" ] || continue
    # settings.json is not a ruleset - it has no .name; skip it here (applied above).
    name="$(jq -r '.name // empty' "$file")"
    [ -n "$name" ] || continue
    # Paginate so a name match on a later page is never missed (which would create a duplicate ruleset), and
    # fail loudly if the API call itself fails (auth/404/network) rather than treating it as "not found".
    if ! ids="$(gh api --paginate "repos/$repo/rulesets" --jq ".[] | select(.name==\"$name\") | .id")"; then
        echo "Failed to list rulesets for $repo (check auth and repo access)." >&2
        exit 1
    fi
    # Pre-existing drift can leave more than one ruleset with the same name; update the first and warn. Guard
    # on non-empty so `grep -c` (which exits non-zero on empty input under `set -e`) can't abort the create path.
    id=""
    if [ -n "$ids" ]; then
        count="$(printf '%s\n' "$ids" | grep -c .)"
        if [ "$count" -gt 1 ]; then
            echo "Warning: $count rulesets named '$name' on $repo; updating the first (resolve the duplicates)." >&2
        fi
        id="$(printf '%s\n' "$ids" | sed -n '1p')"
    fi
    if [ -n "$id" ]; then
        echo "Updating ruleset '$name' (id $id) on $repo"
        gh api --method PUT "repos/$repo/rulesets/$id" --input "$file" >/dev/null
    else
        echo "Creating ruleset '$name' on $repo"
        gh api --method POST "repos/$repo/rulesets" --input "$file" >/dev/null
    fi
done

echo "Configuration applied to $repo"

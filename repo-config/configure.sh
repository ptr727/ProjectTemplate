#!/usr/bin/env bash
# Apply the committed branch rulesets in this directory to the repository via the GitHub API.
# Each <name>.json holds the writable ruleset subset {name, target, enforcement, bypass_actors,
# conditions, rules}. An existing ruleset (matched by name) is updated with a full-payload PUT
# (partial PUTs 422); a missing one is created with POST. Rerunning is idempotent.
#
# Usage: repo-config/configure.sh [owner/repo]   (defaults to the current repo via gh)
set -euo pipefail

repo="${1:-$(gh repo view --json nameWithOwner --jq '.nameWithOwner')}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for file in "$script_dir"/*.json; do
    [ -e "$file" ] || continue
    name="$(jq -r '.name' "$file")"
    # Paginate so a name match on a later page is never missed (which would create a duplicate ruleset), and
    # fail loudly if the API call itself fails (auth/404/network) rather than treating it as "not found".
    if ! id="$(gh api --paginate "repos/$repo/rulesets" --jq ".[] | select(.name==\"$name\") | .id")"; then
        echo "Failed to list rulesets for $repo (check auth and repo access)." >&2
        exit 1
    fi
    if [ -n "$id" ]; then
        echo "Updating ruleset '$name' (id $id) on $repo"
        gh api --method PUT "repos/$repo/rulesets/$id" --input "$file" >/dev/null
    else
        echo "Creating ruleset '$name' on $repo"
        gh api --method POST "repos/$repo/rulesets" --input "$file" >/dev/null
    fi
done

echo "Rulesets applied to $repo"

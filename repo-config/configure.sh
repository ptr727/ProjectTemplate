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

echo "Rulesets applied to $repo"

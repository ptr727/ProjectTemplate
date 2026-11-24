# The newest released Python minor line in peps.python.org's release-cycle data, such as 3.14.
# A status outside the known set fails rather than being guessed released or not.
[ to_entries[]
    | if (.key | test("^[0-9]+\\.[0-9]+$")) then . else error("python: \(.key | tojson) is not a <major>.<minor> line") end
    | .value.status as $status
    | if ($status | IN("bugfix", "security", "end-of-life")) then .key
      elif ($status | IN("feature", "prerelease")) then empty
      else error("python: line \(.key) has an unknown status \($status | tojson)") end
    | split(".") | map(tonumber) ]
| if length == 0 then error("python: the release-cycle data holds no released line") else max | map(tostring) | join(".") end

# The newest released .NET major line in the .NET release index, such as 10.
# A support phase outside the known set fails rather than being guessed released or not.
(if type == "object" then .["releases-index"] else null end) as $releases
| if ($releases | type) != "array" or ($releases | length) == 0 then error("dotnet: the release index holds no releases-index list") else $releases end
| [ .[]
    | if type == "object" then . else error("dotnet: release entry \(tojson) is not an object") end
    | .["channel-version"] as $channel
    | if ($channel | type) == "string" and ($channel | test("^[0-9]+\\.[0-9]+$")) then . else error("dotnet: channel \($channel | tojson) is not a <major>.<minor> line") end
    | .["support-phase"] as $phase
    | if ($phase | IN("active", "maintenance", "eol")) then $channel
      elif ($phase | IN("preview", "go-live")) then empty
      else error("dotnet: channel \($channel) has an unknown support phase \($phase | tojson)") end
    | split(".") | map(tonumber) ]
| if length == 0 then error("dotnet: the release index holds no released line") else max | .[0] | tostring end

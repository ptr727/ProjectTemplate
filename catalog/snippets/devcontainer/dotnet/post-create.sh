#!/usr/bin/env bash
set -Eeuo pipefail

# Restore the .NET local-tool manifest (csharpier, dotnet-outdated).
dotnet tool restore

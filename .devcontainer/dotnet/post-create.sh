#!/usr/bin/env bash
set -euo pipefail

# Restore the .NET local-tool manifest (csharpier, dotnet-outdated).
dotnet tool restore

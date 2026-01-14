#!/bin/sh

# Echo commands
set -x

# Exit on error
set -e

# Print version information
. /etc/os-release; echo $PRETTY_NAME
dotnet --info
/ProjectTemplate/Console --version

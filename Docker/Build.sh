#!/bin/sh

# Echo commands
set -x

# Exit on error
set -e

# Build the solution
dotnet build ./ProjectTemplate/ProjectTemplate.slnx

# Test the solution
dotnet test ./ProjectTemplate/ProjectTemplate.slnx

# Build publish output
dotnet publish ./ProjectTemplate/Console/Console.csproj \
    --arch $TARGETARCH \
    --output ./Build/Release \
    --configuration release \
    -property:PublishAot=false \
    -property:Version=$BUILD_VERSION \
    -property:FileVersion=$BUILD_FILE_VERSION \
    -property:AssemblyVersion=$BUILD_ASSEMBLY_VERSION \
    -property:InformationalVersion=$BUILD_INFORMATION_VERSION \
    -property:PackageVersion=$BUILD_PACKAGE_VERSION
dotnet publish ./ProjectTemplate/Console/Console.csproj \
    --arch $TARGETARCH \
    --output ./Build/Debug \
    --configuration debug \
    -property:PublishAot=false \
    -property:Version=$BUILD_VERSION \
    -property:FileVersion=$BUILD_FILE_VERSION \
    -property:AssemblyVersion=$BUILD_ASSEMBLY_VERSION \
    -property:InformationalVersion=$BUILD_INFORMATION_VERSION \
    -property:PackageVersion=$BUILD_PACKAGE_VERSION

# Copy configured build target as default output
mkdir -p ./Publish/ProjectTemplate/Debug
mkdir -p ./Publish/ProjectTemplate/Release
if [ "$BUILD_CONFIGURATION" = "Debug" ] || [ "$BUILD_CONFIGURATION" = "debug" ]
then
    cp -r ./Build/Debug/* ./Publish/ProjectTemplate
else
    cp -r ./Build/Release/* ./Publish/ProjectTemplate
fi
cp -r ./Build/Debug/* ./Publish/ProjectTemplate/Debug
cp -r ./Build/Release/* ./Publish/ProjectTemplate/Release
ls -la ./Publish/ProjectTemplate

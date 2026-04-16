# Description: Ubuntu latest release
# Based on: ubuntu:rolling
# .NET install: Ubuntu repository
# Platforms: linux/amd64, linux/arm64
# Tag: ptr727/projecttemplate:latest

# Docker build debugging:
# --progress=plain
# --no-cache

# Test image in shell:
# docker run -it --rm --pull always --name Testing ubuntu:rolling /bin/bash
# docker run -it --rm --pull always --name Testing ptr727/projecttemplate:latest /bin/bash
# export DEBIAN_FRONTEND=noninteractive

# Build Dockerfile
# docker buildx create --name "projecttemplate" --use
# docker buildx build --platform linux/amd64,linux/arm64 --file ./Docker/Dockerfile .

# Build and log output
# docker buildx build --no-cache --progress=plain --platform linux/amd64 --file ./Docker/Dockerfile . 2>&1 | tee build.log

# Test linux/amd64 target
# docker buildx build --load --platform linux/amd64 --tag projecttemplate:latest --file ./Docker/Dockerfile .
# docker run -it --rm --name ProjectTemplate-Test projecttemplate:latest /bin/bash


# Builder layer
FROM --platform=$BUILDPLATFORM ubuntu:rolling AS builder

# Layer workdir
WORKDIR /Builder

ARG \
    # Build platform args
    TARGETPLATFORM \
    TARGETARCH \
    BUILDPLATFORM \
    # Build attributes
    BUILD_CONFIGURATION="Debug" \
    BUILD_VERSION="1.0.0.0" \
    BUILD_FILE_VERSION="1.0.0.0" \
    BUILD_ASSEMBLY_VERSION="1.0.0.0" \
    BUILD_INFORMATION_VERSION="1.0.0.0" \
    BUILD_PACKAGE_VERSION="1.0.0.0"

# Prevent EULA and confirmation prompts in installers
ENV DEBIAN_FRONTEND=noninteractive

RUN \
    # Upgrade
    apt update \
    && apt upgrade -y \
    # Install .NET SDK  (for AOT add clang and zlib1g-dev)
    # https://documentation.ubuntu.com/ubuntu-for-developers/howto/dotnet-setup
    # https://learn.microsoft.com/en-us/dotnet/core/install/linux-ubuntu-install
    # https://learn.microsoft.com/en-us/dotnet/core/deploying/native-aot/
    && apt install -y \
        dotnet-sdk-10.0 \
    # Cleanup
    && apt autoremove -y \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

# Copy source
COPY . ./ProjectTemplate/.

# Build project
COPY --chmod=ug=rwx,o=rx ./Docker/Build.sh ./ProjectTemplate
RUN ./ProjectTemplate/Build.sh


# Final layer
FROM ubuntu:rolling AS final

ARG \
    # Build platform args
    TARGETPLATFORM \
    TARGETARCH \
    BUILDPLATFORM \
    # Image label
    LABEL_VERSION="1.0.0.0"

# Label
LABEL name="ProjectTemplate" \
    version=${LABEL_VERSION} \
    description="C# .NET template project." \
    maintainer="Pieter Viljoen <ptr727@users.noreply.github.com>"

# Prevent EULA and confirmation prompts in installers
ENV DEBIAN_FRONTEND=noninteractive

RUN \
    # Upgrade
    apt update \
    && apt upgrade -y \
    # Install dependencies
    && apt install -y \
        ca-certificates \
        locales \
        locales-all \
        p7zip-full \
        tzdata \
        wget \
    && locale-gen --no-purge en_US en_US.UTF-8 \
    # Install .NET Runtime
    && apt install -y \
        dotnet-runtime-10.0 \
    # Cleanup
    && apt autoremove -y \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

# Set locale to UTF-8 after running locale-gen
# https://github.com/dotnet/dotnet-docker/blob/main/samples/enable-globalization.md
ENV TZ=Etc/UTC \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8

# Copy build output from builder layer
COPY --from=builder /Builder/Publish/ProjectTemplate/. /ProjectTemplate

# Install debug tools
COPY --chmod=ug=rwx,o=rx ./Docker/InstallDebugTools.sh ./ProjectTemplate
RUN ./ProjectTemplate/InstallDebugTools.sh \
    && rm -rf ./ProjectTemplate/InstallDebugTools.sh

# Print environment information
COPY --chmod=ug=rwx,o=rx ./Docker/Version.sh ./ProjectTemplate
RUN if [ "$BUILDPLATFORM" = "$TARGETPLATFORM" ]; then \
        /ProjectTemplate/Version.sh; \
    fi \
    && rm -rf ./ProjectTemplate/Version.sh

# Set workdir
WORKDIR /ProjectTemplate

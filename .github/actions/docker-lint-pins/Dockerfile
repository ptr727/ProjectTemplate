# The Docker lint images, read by action.yml beside this file, by repo_gate.py, and by scripts/docker_lint.py.
# It is never built, and is a Dockerfile only so Dependabot's docker ecosystem bumps each tag and its digest.
FROM ghcr.io/editorconfig-checker/editorconfig-checker:4.0.2@sha256:2ba6232bfa0058f72f5f7d7816711590aa764afab1542af30b3ecb4553587918 AS editorconfig-checker
FROM rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667 AS actionlint
FROM davidanson/markdownlint-cli2:v0.24.2@sha256:c768e94d8bb161c449bde28f60656929ca1f02c3010a405f65c9cac71563a889 AS markdownlint
FROM ghcr.io/streetsidesoftware/cspell:10.5.2@sha256:d16ebc7f7b81414c500f279b0f96c43a8c1bfa04279b9b8d98d13c300e2b7095 AS cspell
FROM mirror.gcr.io/koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d AS shellcheck
FROM mirror.gcr.io/mvdan/shfmt:v3.14.1@sha256:8c06884a35683d8763fba6c0d9484d11a61a96da65f8d497d6f625825a6043f2 AS shfmt
FROM mcr.microsoft.com/powershell:7.4-ubuntu-22.04@sha256:62300a213a9293916333df2b014cd3a8f22fb0b0b65f2bb446aaf436bcf8c868 AS powershell

#!/bin/sh
# Shared by both image paths. Build against Bookworm libraries only; keep all
# compiler/header packages in this throwaway stage, never in the runtime image.
set -eu
GIT_VERSION=2.55.0
# kernel.org sha256sums.asc; independently checked against the downloaded archive.
GIT_SHA256=457fdb04dc8728e007d4688695e6912e6f680727920f2a40bf11eacc17505357
apt-get update
apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl xz-utils perl pkg-config \
    libcurl4-openssl-dev libssl-dev libexpat1-dev zlib1g-dev
mkdir -p /build/git
cd /build/git
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    "https://www.kernel.org/pub/software/scm/git/git-${GIT_VERSION}.tar.xz" -o git.tar.xz
printf '%s  git.tar.xz\n' "$GIT_SHA256" | sha256sum -c -
tar -xJf git.tar.xz --strip-components=1
# Git 2.55's optional Rust implementation is unnecessary for these commands.
make -j2 prefix=/opt/git NO_TCLTK=YesPlease NO_GETTEXT=YesPlease NO_RUST=YesPlease all
make prefix=/opt/git NO_TCLTK=YesPlease NO_GETTEXT=YesPlease NO_RUST=YesPlease install
test "$(/opt/git/bin/git --version)" = "git version ${GIT_VERSION}"
test -x /opt/git/libexec/git-core/git-remote-https
# Retain the upstream license with the installed artifact.
install -m 644 COPYING /opt/git/COPYING

#!/bin/bash
# slopetanium-ui for the handhelds (aarch64), in Docker: port/slopetanium/slopetanium-ui.aarch64
set -euo pipefail
cd "$(dirname "$0")/.."
docker image inspect slopetanium-cross:bullseye >/dev/null 2>&1 || \
  docker build -t slopetanium-cross:bullseye -f docker/Dockerfile.cross docker
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD/ui:/src" -w /src slopetanium-cross:bullseye \
  make CROSS=aarch64-linux-gnu- OUT=slopetanium-ui.aarch64 -j"$(nproc)"
cp ui/slopetanium-ui.aarch64 port/slopetanium/slopetanium-ui.aarch64
ls -l port/slopetanium/slopetanium-ui.aarch64

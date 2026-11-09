#!/bin/bash
# Build dist/slopetanium.zip, a PortMaster port zip (Slopetanium.sh + slopetanium/).
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf dist/stage && mkdir -p dist/stage
cp port/Slopetanium.sh dist/stage/
mkdir -p dist/stage/slopetanium
cp -r port/slopetanium/app port/slopetanium/web port/slopetanium/port.json \
      port/slopetanium/gameinfo.xml port/slopetanium/README.md port/slopetanium/LICENSE \
      port/slopetanium/assets dist/stage/slopetanium/
[ -f port/slopetanium/screenshot.png ] && cp port/slopetanium/screenshot.png dist/stage/slopetanium/
for b in port/slopetanium/slopetanium-ui.*; do [ -f "$b" ] && cp "$b" dist/stage/slopetanium/ && chmod +x "dist/stage/slopetanium/$(basename "$b")"; done
mkdir -p dist/stage/slopetanium/licenses && cp ui/third_party/LICENSE.BananaLinux ui/third_party/README.md dist/stage/slopetanium/licenses/
find dist/stage -name __pycache__ -prune -exec rm -rf {} +
chmod +x dist/stage/Slopetanium.sh dist/stage/slopetanium/app/slopetanium.py
rm -f dist/slopetanium.zip
(cd dist/stage && zip -qr -X ../slopetanium.zip "Slopetanium.sh" slopetanium)
md5sum dist/slopetanium.zip | cut -d' ' -f1 > dist/slopetanium.zip.md5
ls -l dist/slopetanium.zip

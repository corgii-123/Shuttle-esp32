#!/usr/bin/env bash
# Regenerate firmware/upstream.patch = diff of patched upstream files vs. the vendored upstream commit.
set -euo pipefail
cd "$(dirname "$0")/.."
UPSTREAM_COMMIT=$(grep -oE '[0-9a-f]{40}' firmware/UPSTREAM.md | head -1)
TMP=$(mktemp -d)
git clone -q https://github.com/78/xiaozhi-esp32.git "$TMP/x"
git -C "$TMP/x" checkout -q "$UPSTREAM_COMMIT"
FILES="CMakeLists.txt main/CMakeLists.txt main/Kconfig.projbuild main/application.cc main/boards/waveshare/esp32-s3-epaper-1.54/custom_lcd_display.cc"
: > firmware/upstream.patch
for f in $FILES; do
  diff -u --label "a/$f" --label "b/$f" "$TMP/x/$f" "firmware/$f" >> firmware/upstream.patch || true
done
rm -rf "$TMP"
echo "wrote firmware/upstream.patch ($(grep -c '^@@' firmware/upstream.patch) hunks)"

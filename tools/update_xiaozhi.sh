#!/usr/bin/env bash
# Re-vendor xiaozhi-esp32 at <ref> into firmware/ and re-apply Shuttle's patch.
#   tools/update_xiaozhi.sh v2.6.0        (commit or tag)
# Run on a clean git tree; review `git diff` afterwards.
set -euo pipefail
REF="${1:?usage: tools/update_xiaozhi.sh <commit-or-tag>}"
cd "$(dirname "$0")/.."
[[ -z $(git status --porcelain firmware) ]] || { echo "firmware/ has uncommitted changes"; exit 1; }
TMP=$(mktemp -d)
git clone -q https://github.com/78/xiaozhi-esp32.git "$TMP/x"
git -C "$TMP/x" checkout -q "$REF"
NEW=$(git -C "$TMP/x" rev-parse HEAD)

# Keep Shuttle-owned paths, replace everything else.
KEEP="extra_components main/boards/shuttle build_shuttle.py shuttle.local.conf.example shuttle.local.conf UPSTREAM.md upstream.patch"
mkdir -p "$TMP/keep"
for k in $KEEP; do [[ -e firmware/$k ]] && { mkdir -p "$TMP/keep/$(dirname "$k")"; cp -a "firmware/$k" "$TMP/keep/$k"; }; done
find firmware -mindepth 1 -maxdepth 1 ! -name build ! -name managed_components -exec rm -rf {} +
(cd "$TMP/x" && tar --exclude=.git --exclude=.github --exclude=docker -cf - .) | (cd firmware && tar xf -)
cp -a "$TMP/keep/." firmware/

echo "== applying firmware/upstream.patch"
if git apply --reject --whitespace=nowarn --directory=firmware firmware/upstream.patch; then
  echo "patch applied cleanly"
else
  echo "!! conflicts - resolve them (see *.rej files), then run tools/make_upstream_patch.sh"
fi
sed -i.bak -E "s/[0-9a-f]{40}/$NEW/" firmware/UPSTREAM.md && rm -f firmware/UPSTREAM.md.bak
rm -rf "$TMP"
echo "Now: compare main/boards/waveshare/esp32-s3-epaper-1.54/*.cc changes with shuttle_board.cc, build, test."

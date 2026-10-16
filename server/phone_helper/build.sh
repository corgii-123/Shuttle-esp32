#!/usr/bin/env bash
# Build shuttle-helper.jar (a dex jar run with app_process; nothing gets installed).
# Needs the Android SDK: $ANDROID_HOME with platforms/android-XX and build-tools/<ver>/d8.
#   macOS: brew install --cask android-commandlinetools
#          sdkmanager "platforms;android-35" "build-tools;35.0.0"
set -euo pipefail
cd "$(dirname "$0")"
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
PLATFORM=$(ls -d "$SDK"/platforms/android-* 2>/dev/null | sort -V | tail -1)
BUILD_TOOLS=$(ls -d "$SDK"/build-tools/* 2>/dev/null | sort -V | tail -1)
[ -f "$PLATFORM/android.jar" ] || { echo "android.jar not found under $SDK/platforms"; exit 1; }
[ -x "$BUILD_TOOLS/d8" ] || { echo "d8 not found under $SDK/build-tools"; exit 1; }
rm -rf build && mkdir -p build/classes
javac --release 8 -Xlint:-options -cp "$PLATFORM/android.jar" -d build/classes $(find src -name '*.java')
"$BUILD_TOOLS/d8" --release --min-api 24 --lib "$PLATFORM/android.jar" --output build/ \
    $(find build/classes -name '*.class')
cd build && zip -q -X ../../shuttle_server/phone_helper/shuttle-helper.jar classes.dex && cd ..
echo "built server/shuttle_server/phone_helper/shuttle-helper.jar ($(wc -c < ../shuttle_server/phone_helper/shuttle-helper.jar) bytes)"

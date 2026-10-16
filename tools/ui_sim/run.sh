#!/usr/bin/env bash
# Render the Shuttle e-paper UI on the desktop (no hardware needed).
#   tools/ui_sim/run.sh            -> docs/ui/screens.png + docs/ui/*.gif
# Fetches LVGL v9.5 and xiaozhi-fonts into ~/.cache/shuttle-ui-sim on first run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CACHE="${SHUTTLE_UI_SIM_CACHE:-$HOME/.cache/shuttle-ui-sim}"
BOARD="$REPO/firmware/main/boards/shuttle/epaper-1.54-v2"
PY="${PYTHON:-python3}"
mkdir -p "$CACHE"
[[ -d $CACHE/lvgl ]] || git clone -q --depth 1 -b release/v9.5 https://github.com/lvgl/lvgl.git "$CACHE/lvgl"
[[ -d $CACHE/fonts ]] || git clone -q --depth 1 https://github.com/78/xiaozhi-fonts.git "$CACHE/fonts"

"$PY" "$REPO/tools/mascot/gen_mascot.py"

if [[ ! -f $CACHE/liblvgl.a || $HERE/lv_conf.h -nt $CACHE/liblvgl.a ]]; then
  echo "building LVGL (once)..."
  rm -rf "$CACHE/obj" && mkdir -p "$CACHE/obj"
  i=0
  find "$CACHE/lvgl/src" -name '*.c' | while read -r f; do
    i=$((i + 1))
    echo "cc -O1 -w -c -DLV_CONF_INCLUDE_SIMPLE -I$HERE -I$CACHE/lvgl $f -o $CACHE/obj/$i.o"
  done | xargs -P 8 -I{} sh -c '{}'
  ar rcs "$CACHE/liblvgl.a" "$CACHE"/obj/*.o
fi

FONTS="$CACHE/fonts/src"
CFLAGS=(-O1 -w -DLV_CONF_INCLUDE_SIMPLE -DLV_LVGL_H_INCLUDE_SIMPLE -I"$HERE" -I"$CACHE/lvgl" -I"$CACHE/fonts/include")
OBJS=()
for f in font_noto_sans_basic_14_1 font_noto_sans_basic_20_4 font_material_symbols_14_1; do
  cc "${CFLAGS[@]}" -c "$FONTS/$f.c" -o "$CACHE/$f.o"
  OBJS+=("$CACHE/$f.o")
done
c++ -std=c++17 "${CFLAGS[@]}" -I"$BOARD" "$HERE/ui_sim.cc" "$BOARD/shuttle_ui.cc" "${OBJS[@]}" \
  "$CACHE/liblvgl.a" -lm -o "$CACHE/ui_sim"

OUT="$CACHE/out"; rm -rf "$OUT"; mkdir -p "$OUT"
"$CACHE/ui_sim" "$CACHE/fonts/cbin/font_noto_sans_common_20_4.bin" "$OUT"
"$PY" "$HERE/render.py" "$OUT" "$REPO/docs/ui"

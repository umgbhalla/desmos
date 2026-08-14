#!/usr/bin/env bash
# Record a high-resolution desmos TUI demo with termctrl.
#
# Resolution is cell geometry, not a flag:
#   width  = (cols * cell_width  + 2 * padding) * pixel_ratio
#   height = (rows * cell_height + 2 * padding) * pixel_ratio
#
# The defaults below are measured, not guessed: 140x38 cells at 9x18 with
# padding 20 and pixel_ratio 2 exports 2600x1448, verified with ffprobe.
# Raise --pixel-ratio to 3 for 3900x2172 if you want to downscale to 1080p
# without resampling artifacts on the glyph edges.
set -euo pipefail

NAME="${NAME:-desmos-demo}"
SCENE="${1:?usage: record-demo.sh <scene> [--live]}"
LIVE="${2:-}"

COLS="${COLS:-140}"
ROWS="${ROWS:-38}"
CELL_W="${CELL_W:-9}"
CELL_H="${CELL_H:-18}"
PADDING="${PADDING:-20}"
RATIO="${RATIO:-2}"
FPS="${FPS:-60}"
PACE="${PACE:-32}"

OUT_DIR="${OUT_DIR:-captures}"
REC="$OUT_DIR/$SCENE.termctrl"
MP4="$OUT_DIR/$SCENE.mp4"
mkdir -p "$OUT_DIR"

echo "geometry: $((COLS*CELL_W+2*PADDING))x$((ROWS*CELL_H+2*PADDING)) logical" \
     "-> $(( (COLS*CELL_W+2*PADDING)*RATIO ))x$(( (ROWS*CELL_H+2*PADDING)*RATIO )) at ratio $RATIO"

if [ "$LIVE" = "--live" ]; then
  CMD=(python -m desmos tui)
else
  CMD=(./target/release/desmos-tui --demo)
  [ -x ./target/release/desmos-tui ] || CMD=(./target/debug/desmos-tui --demo)
fi

termctrl stop "$NAME" >/dev/null 2>&1 || true
termctrl prune >/dev/null 2>&1 || true
termctrl start "$NAME" \
  --cols "$COLS" --rows "$ROWS" \
  --cell-width "$CELL_W" --cell-height "$CELL_H" \
  --record "$REC" --cwd "$PWD" -- "${CMD[@]}"

# ---- scenes -----------------------------------------------------------------
# Each scene drives the TUI and drops markers. Keep them short: the point is to
# show one idea per clip, not to tour the whole app.

s() { termctrl send "$NAME" "$@" >/dev/null; }
m() { termctrl mark "$NAME" "$1" >/dev/null; sleep 0.4; }

# Two panes: prose on the left, the syscalls that produced it on the right.
scene_panes() {
  m open
  s tab;            sleep 1.2; m wire
  s text:j text:j text:j; sleep 1.0; m select
  s text:l;         sleep 1.4; m expand
  s text:h;         sleep 0.8; m fold
  s tab tab;        sleep 1.2; m posts
  s text:j text:j text:l; sleep 1.4; m tree
  m end
}

# The wire tail: newest cards open, older ones folded, overflow counted.
scene_wire() {
  m open
  s tab;            sleep 1.0; m focus
  s page-up;        sleep 1.2; m scrolled
  s page-down;      sleep 1.2; m tail
  m end
}

# Zoom one block into the pager: search, wrap, raw.
scene_zoom() {
  m open
  s tab text:j text:j; sleep 1.0; m selected
  s enter;          sleep 1.4; m zoomed
  s text:/ text:cache enter; sleep 1.4; m searched
  s escape;         sleep 1.0; m closed
  m end
}

# Each scene is a function so the marker names line up with the edit plan.
sleep 2
"scene_$SCENE"

termctrl stop "$NAME" >/dev/null 2>&1 || true

VIDEO_ARGS=(--fps "$FPS" --pixel-ratio "$RATIO" --padding "$PADDING"
            --hide-cursor --tail-ms 600 -o "$MP4")
[ -f "$OUT_DIR/$SCENE.json" ] && VIDEO_ARGS+=(--edit "$OUT_DIR/$SCENE.json" --footer)

termctrl video "$REC" "${VIDEO_ARGS[@]}"
ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate,nb_frames \
  -of default=nw=1 "$MP4"

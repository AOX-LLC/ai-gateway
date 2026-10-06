#!/usr/bin/env bash
# Mux a recorded voice-over onto a silent cut, into the finished MP4 (it goes to YouTube; it is not committed).
#   demo/video/mux.sh <voice-file> [core|extended] [--embed-captions]
# The voice file is whatever the recorder made (WAV, M4A, MP3 ...), recorded while watching the teleprompter
# cut, starting when the video starts. The picture is copied, never re-encoded. The loudness is normalised in
# two passes (EBU R128: -16 LUFS integrated, -1.5 dB true peak, the usual target for online video): the first
# pass measures, the second applies those measurements linearly where the true-peak target allows it (loudnorm
# falls back to its dynamic mode otherwise), and the result is measured and printed. A voice more
# than HALF_SECOND longer than the picture is refused (the recording ran over its script); a shorter one is
# padded with silence to the picture's length. --embed-captions adds docs/video/captions-<cut>.srt as a soft
# subtitle track (YouTube takes the .srt or .vtt separately too).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
DEMO=$(cd "$HERE/.." && pwd)
REPO=$(cd "$DEMO/.." && pwd)
HALF_SECOND=0.5
TARGET_I=-16
TARGET_TP=-1.5
TARGET_LRA=11

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" >&2; exit 2; }
[ $# -ge 1 ] || usage
VOICE=$1
CUT=${2:-core}
EMBED=${3:-}
case "$CUT" in core|extended) ;; *) echo "the cut must be core or extended, got: $CUT" >&2; exit 2 ;; esac
case "$EMBED" in ""|--embed-captions) ;; *) echo "unknown option: $EMBED (the only one is --embed-captions)" >&2; exit 2 ;; esac
SILENT="$DEMO/out/video/silent-$CUT.mp4"
CAPTIONS="$REPO/docs/video/captions-$CUT.srt"
OUT="$DEMO/out/final/ai-gateway-$CUT.mp4"
[ -f "$VOICE" ] || { echo "no such voice file: $VOICE" >&2; exit 2; }
[ -f "$SILENT" ] || { echo "no silent cut at $SILENT: run 'cd demo && npm run video' first" >&2; exit 2; }
mkdir -p "$DEMO/out/final"

duration() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }
VIDEO_S=$(duration "$SILENT")
VOICE_S=$(duration "$VOICE")
python3 - "$VIDEO_S" "$VOICE_S" "$HALF_SECOND" <<'PY'
import sys
video, voice, slack = (float(x) for x in sys.argv[1:4])
print(f"picture {video:.1f} s, voice {voice:.1f} s")
if voice > video + slack:
    sys.exit(f"the voice is {voice - video:.1f} s longer than the picture: re-record it to the script's timings")
PY

echo "pass 1: measuring the voice"
MEASURE=$(ffmpeg -hide_banner -nostats -i "$VOICE" \
  -af "loudnorm=I=$TARGET_I:TP=$TARGET_TP:LRA=$TARGET_LRA:print_format=json" -f null - 2>&1)
READER=$(mktemp)
trap 'rm -f "$READER"' EXIT
cat > "$READER" <<'PY'
import json
import re
import sys

target_i, target_tp, target_lra = sys.argv[1:4]
block = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", sys.stdin.read(), re.S)
if not block:
    sys.exit("could not read the loudness measurement")
m = json.loads(block.group(0))
parts = [
    f"loudnorm=I={target_i}", f"TP={target_tp}", f"LRA={target_lra}",
    "measured_I=" + m["input_i"], "measured_TP=" + m["input_tp"], "measured_LRA=" + m["input_lra"],
    "measured_thresh=" + m["input_thresh"], "offset=" + m["target_offset"], "linear=true",
]
print(":".join(parts))
PY
FILTER=$(python3 "$READER" "$TARGET_I" "$TARGET_TP" "$TARGET_LRA" <<<"$MEASURE")
echo "pass 2: applying $FILTER"

INPUTS=(-i "$SILENT" -i "$VOICE")
MAPS=(-map 0:v -map "[voice]")
SUBTITLES=()
if [ "$EMBED" = "--embed-captions" ]; then
  [ -f "$CAPTIONS" ] || { echo "no captions at $CAPTIONS" >&2; exit 2; }
  INPUTS+=(-i "$CAPTIONS")
  MAPS+=(-map 2:s)
  SUBTITLES=(-c:s mov_text -metadata:s:s:0 language=eng)
fi
ffmpeg -hide_banner -loglevel error -y "${INPUTS[@]}" \
  -filter_complex "[1:a]${FILTER},aresample=48000,apad[voice]" "${MAPS[@]}" \
  -c:v copy -c:a aac -b:a 192k "${SUBTITLES[@]}" -t "$VIDEO_S" -movflags +faststart "$OUT"
echo "wrote $OUT ($(duration "$OUT") s)"
echo "result, measured:"
ffmpeg -hide_banner -nostats -i "$OUT" -vn -af ebur128=peak=true -f null - 2>&1 | grep -E "^\s+(I|Peak):" | tail -2

#!/bin/bash
# Transcript and key frames of a YouTube video the owner's Mac fetched into ~/private/yt-inbox (scripts/mac/yt_fetch.sh),
# because YouTube blocks the server. Subtitles when the video has them, else Whisper on the CPU; then an overview of
# frames next to the transcript. Output: ~/private/yt-inbox/<id>/{transcript.md,frames/}.
#   bash scripts/yt_local.sh VIDEO_ID [SECONDS_PER_PARAGRAPH, default 60; 15 to pick key frames]
set -euo pipefail
ID="${1:?usage: yt_local.sh VIDEO_ID [SECONDS]}"
EVERY="${2:-60}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
IN="$HOME/private/yt-inbox"
OUT="$IN/$ID"
URL="https://www.youtube.com/watch?v=$ID"
mkdir -p "$OUT"
vtt="$(ls "$IN/$ID".*.vtt 2>/dev/null | grep -E '\.(ru|uk|ru-orig|uk-orig)\.vtt$' | head -1 || true)"
[[ -n "$vtt" ]] || vtt="$(ls "$IN/$ID".*.vtt 2>/dev/null | head -1 || true)"
cd "$REPO"
if [[ -n "$vtt" ]]; then
  echo "transcript from subtitles: $(basename "$vtt")"
  uv run --no-project python -I scripts/yt_transcript.py "$URL" --vtt "$vtt" --every "$EVERY" --out "$OUT/transcript.md"
else
  echo "no subtitles: recognising the speech on the CPU (about the video's length)"
  HF_HOME="$HOME/.cache/huggingface" uv run --no-project --with faster-whisper --with imageio-ffmpeg --with numpy \
    python -I scripts/yt_asr.py "$URL" --cache "$IN" --every "$EVERY" --out "$OUT/transcript.md"
fi
rm -rf "$OUT/frames"
uv run --no-project --with 'yt-dlp[default]' --with imageio-ffmpeg --with pillow \
  python -I scripts/yt_frames.py "$URL" --cache "$IN" --transcript "$OUT/transcript.md" --out "$OUT/frames"
echo "ok: $OUT/transcript.md, $OUT/frames/index.md"

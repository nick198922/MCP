#!/bin/bash
# Run on the owner's Mac (Apple Silicon): recognises the speech of a YouTube video without subtitles with mlx-whisper,
# tens of times faster than the server's CPU, and sends only the transcript to the server
# (~/private/yt-inbox/<id>/transcript.md), where Claude takes the frames. Kept on the Mac: the Whisper model
# (~1.6 GB, ~/.cache/huggingface, once); the work folder is deleted.
#   ssh tradedesk@169.58.169.184 cat tradedesk-mcp/scripts/mac/yt_asr.sh | bash -s 'https://www.youtube.com/watch?v=…'
# The script itself arrives on stdin: everything sits in a function, ssh/scp get /dev/null as stdin.
set -euo pipefail
SERVER="tradedesk@${TRADEDESK_SERVER:-169.58.169.184}"
TD_DIR="${TD_DIR:-tradedesk-mcp}"   # the server clone whose scripts run here

asr_main() {
  local url="${1:?usage: yt_asr.sh URL [SECONDS_PER_PARAGRAPH, default 15]}" every="${2:-15}" id work
  export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
  if [[ "$(uname -m)" != arm64 ]]; then
    echo "this Mac is not Apple Silicon: mlx-whisper will not run, the server will recognise the speech itself"
    exit 1
  fi
  if ! command -v uv >/dev/null; then
    echo "installing uv (once)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  fi
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  ssh -n "$SERVER" "tar -C $TD_DIR/scripts -czf - yt_asr.py yt_transcript.py" | tar -C "$work" -xzf -
  echo "recognising the speech (first run downloads the model, ~1.6 GB)"
  awake uv run -q --no-project --python 3.12 --with 'yt-dlp[default]' --with deno --with mlx-whisper --with imageio-ffmpeg \
    --with numpy python -I "$work/yt_asr.py" "$url" --cache "$work" --every "$every" --out "$work/transcript.md" </dev/null
  id="$(sed -n 's/.*watch?v=\([A-Za-z0-9_-]*\).*/\1/p' <<<"$url")"
  id="${id:-video}"
  ssh -n "$SERVER" "mkdir -p private/yt-inbox/$id && chmod 700 private/yt-inbox/$id"
  scp -q "$work/transcript.md" "$SERVER:private/yt-inbox/$id/transcript.md" </dev/null
  echo "ok: скажіть Claude «текст відео $id на сервері»."
}

awake() { if command -v caffeinate >/dev/null; then caffeinate -i "$@"; else "$@"; fi; }   # no sleep during a long run

asr_main "$@"

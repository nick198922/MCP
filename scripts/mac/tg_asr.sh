#!/bin/bash
# Run on the owner's Mac (Apple Silicon): recognises the speech in a Telegram export's videos with mlx-whisper, tens of
# times faster than the server's CPU, and sends only the text (<video>/segments.json) to the server, where
# scripts/tg_videos.py reuses it for the transcripts and takes the key frames. Videos already on the server are skipped;
# after a break run the same line again. Kept on the Mac: the Whisper model (~1.6 GB, ~/.cache/huggingface, once);
# the work folder ~/.cache/tradedesk-tg-asr/<export> is deleted after the upload.
#   ssh tradedesk@169.58.169.184 cat tradedesk-mcp/scripts/mac/tg_upload.sh tradedesk-mcp/scripts/mac/tg_asr.sh | bash -s
# (copy the export first, then recognise; tg_asr.sh alone works too). Without a path it takes the newest ChatExport_*.
# The script itself arrives on stdin: everything sits in a function, ssh/rsync get /dev/null as stdin.
set -euo pipefail
SERVER="tradedesk@${TRADEDESK_SERVER:-169.58.169.184}"
TD_DIR="${TD_DIR:-tradedesk-mcp}"   # the server clone whose scripts run here

asr_main() {
  local src="${1:-}" name work py done_list
  export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
  if [[ -z "$src" ]]; then
    src="$(find "$HOME/Downloads" "$HOME/Desktop" "$HOME/Documents" -maxdepth 3 -type d -name 'ChatExport*' -print0 2>/dev/null |
      xargs -0 stat -f '%m%t%N' 2>/dev/null | sort -rn | head -1 | cut -f2- || true)"
  fi
  if [[ -z "$src" || ! -d "$src" ]]; then
    echo "no ChatExport_* folder found: add its path after -s"
    exit 1
  fi
  src="${src%/}"
  if [[ "$(uname -m)" != arm64 ]]; then
    echo "this Mac is not Apple Silicon: mlx-whisper will not run, the server will recognise the speech itself"
    exit 1
  fi
  name="$(basename "$src" | tr -c 'A-Za-z0-9._\n-' '_')"
  if ! command -v uv >/dev/null; then
    echo "installing uv (once)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  fi

  work="$HOME/.cache/tradedesk-tg-asr/$name"
  py="$work/scripts"
  mkdir -p "$py" "$work/out"
  ssh -n "$SERVER" "tar -C $TD_DIR/scripts -czf - tg_videos.py yt_asr.py yt_transcript.py yt_frames.py" | tar -C "$py" -xzf -
  # Mark what the server already has, so it is not recognised again (empty placeholders, removed before the upload).
  done_list="$(ssh -n "$SERVER" "mkdir -p private/tg-inbox/$name.asr && cd private/tg-inbox/$name.asr && ls")"
  while IFS= read -r d; do
    if [[ -n "$d" && ! -s "$work/out/$d/segments.json" ]]; then mkdir -p "$work/out/$d" && : >"$work/out/$d/segments.json"; fi
  done <<<"$done_list"

  echo "recognising the speech of $src/video_files (first run downloads the model, ~1.6 GB)"
  awake uv run -q --no-project --python 3.12 --with mlx-whisper --with imageio-ffmpeg --with numpy \
    python -I "$py/tg_videos.py" "$src" --out "$work/out" --asr-only </dev/null

  find "$work/out" -name segments.json -empty -delete
  find "$work/out" -type d -empty -delete
  if [[ -d "$work/out" ]]; then
    rsync -a "$work/out/" "$SERVER:private/tg-inbox/$name.asr/" </dev/null
  fi
  ssh -n "$SERVER" "chmod -R go-rwx private/tg-inbox/$name.asr; ls private/tg-inbox/$name.asr | wc -l" | sed 's/^/videos with text on the server: /'
  rm -rf "$work"
  echo "ok: скажіть Claude «текст відео на сервері»."
}

awake() { if command -v caffeinate >/dev/null; then caffeinate -i "$@"; else "$@"; fi; }   # no sleep during a long run

asr_main "$@"

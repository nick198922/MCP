#!/bin/bash
# Run on the owner's Mac when Telegram says a new channel video needs the Mac (scripts/channel_watch.py): takes the
# video he has just saved from Telegram («Зберегти як…», the newest video in ~/Downloads of the last 3 days), copies it
# to the server as the post's video, and leaves the speech recognition to tg_asr.sh, which runs right after this part:
#   ssh tradedesk@169.58.169.184 cat tradedesk-mcp/scripts/mac/tg_live_video.sh tradedesk-mcp/scripts/mac/tg_asr.sh | bash -s DataHawks 1140
# The server's watcher picks the text up within 20 minutes (key frames, then Claude's analysis, then Telegram).
# The script arrives on stdin: ssh/rsync get /dev/null as stdin.
set -euo pipefail
SERVER="tradedesk@${TRADEDESK_SERVER:-169.58.169.184}"

live_main() {
  local channel="${1:-}" post="${2:-}" video dir
  if [[ -z "$channel" || ! "$post" =~ ^[0-9]+$ ]]; then
    echo "usage: … | bash -s <channel> <post number>"
    exit 1
  fi
  video="$(find "$HOME/Downloads" -maxdepth 3 -type f \( -iname '*.mp4' -o -iname '*.mov' -o -iname '*.m4v' \) -mtime -3 -print0 2>/dev/null |
    xargs -0 stat -f '%m%t%N' 2>/dev/null | sort -rn | head -1 | cut -f2- || true)"
  if [[ -z "$video" ]]; then
    echo "no video saved in ~/Downloads in the last 3 days: in Telegram right-click the video → «Зберегти як…» → Downloads"
    exit 1
  fi
  echo "video: $video ($(du -h "$video" | cut -f1))"
  dir="$HOME/.cache/tradedesk-tg-live/$channel-live"
  rm -rf "$dir"
  mkdir -p "$dir/video_files"
  cp "$video" "$dir/video_files/$post.mp4"
  ssh -n "$SERVER" "mkdir -p private/tg-inbox/$channel-live/video_files && chmod -R go-rwx private/tg-inbox/$channel-live"
  echo "copying to the server…"
  rsync -a --partial "$dir/video_files/$post.mp4" "$SERVER:private/tg-inbox/$channel-live/video_files/" </dev/null
  LIVE_EXPORT="$dir"
}

live_main "$@"
set -- "$LIVE_EXPORT"   # tg_asr.sh, appended after this file, recognises this folder

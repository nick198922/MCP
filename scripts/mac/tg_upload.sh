#!/bin/bash
# Run on the owner's Mac: copies a Telegram Desktop channel export (ChatExport_*: result.json or messages*.html plus
# photos/, video_files/, files/ …) to the server, where Claude transcribes the videos, takes their key frames and reads
# the pictures. Resumable: run the same line again after a break and rsync continues. Nothing on the Mac is changed.
#   ssh tradedesk@169.58.169.184 cat tradedesk-mcp/scripts/mac/tg_upload.sh | bash -s ['/path/to/ChatExport_…']
# Without a path it takes the newest ChatExport_* folder under ~/Downloads, ~/Desktop or ~/Documents.
# Server side: ~/private/tg-inbox/<folder>/ (mode 700, never in git); .uploaded marks a finished copy.
set -euo pipefail
SERVER="tradedesk@${TRADEDESK_SERVER:-169.58.169.184}"

src="${1:-}"
if [[ -z "$src" ]]; then
  src="$(find "$HOME/Downloads" "$HOME/Desktop" "$HOME/Documents" -maxdepth 3 -type d -name 'ChatExport*' -print0 2>/dev/null |
    xargs -0 stat -f '%m%t%N' 2>/dev/null | sort -rn | head -1 | cut -f2- || true)"
fi
if [[ -z "$src" || ! -d "$src" ]]; then
  echo "no ChatExport_* folder found: add its path after -s (drag the folder into Terminal)"
  exit 1
fi
src="${src%/}"
if [[ ! -f "$src/result.json" && ! -f "$src/messages.html" ]]; then
  echo "$src: no result.json or messages.html inside, is it a Telegram export?"
  exit 1
fi

name="$(basename "$src" | tr -c 'A-Za-z0-9._\n-' '_')"
echo "export: $src ($(du -sh "$src" | cut -f1), $(find "$src" -type f | wc -l | tr -d ' ') files)"
for d in photos video_files round_video_messages voice_messages files; do
  if [[ -d "$src/$d" ]]; then echo "  $d: $(find "$src/$d" -type f | wc -l | tr -d ' ')"; fi
done
skipped="$(cat "$src"/result.json "$src"/messages*.html 2>/dev/null | grep -o -i -E 'not included|exceeds maximum size' | wc -l | tr -d ' ' || true)"
if [[ "$skipped" != 0 ]]; then
  echo "WARNING: $skipped media were left out by the export's size limit. Copying the rest now; for the missing ones"
  echo "  export again with Size limit at maximum (Videos and Files ticked), then run this line with the new folder."
fi

echo "copying to $SERVER:private/tg-inbox/$name/ (run the same line again if it stops)"
ssh "$SERVER" 'umask 077; mkdir -p ~/private/tg-inbox; chmod 700 ~/private'
awake() { if command -v caffeinate >/dev/null; then caffeinate -i "$@"; else "$@"; fi; }   # no sleep during a long copy
awake rsync -a --partial --progress --exclude .DS_Store "$src/" "$SERVER:private/tg-inbox/$name/"

local_n="$(find "$src" -type f ! -name .DS_Store | wc -l | tr -d ' ')"
remote_n="$(ssh "$SERVER" "find private/tg-inbox/$name -type f ! -name .uploaded | wc -l")"
if [[ "$local_n" != "$remote_n" ]]; then
  echo "copied $remote_n of $local_n files: run the same line again"
  exit 1
fi
ssh "$SERVER" "chmod -R go-rwx private/tg-inbox/$name; date -u +%FT%TZ > private/tg-inbox/$name/.uploaded; du -sh private/tg-inbox/$name"
echo "ok: $name ($remote_n files). Скажіть Claude: «експорт $name на сервері»."

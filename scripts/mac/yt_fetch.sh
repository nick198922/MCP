#!/bin/bash
# Run on the owner's Mac (a home IP; YouTube blocks the server). Downloads a YouTube video for the library and copies
# it to the server, where scripts/yt_local.sh makes the transcript and the frames. Nothing is kept on the Mac.
#   ssh tradedesk@169.58.169.184 cat tradedesk-mcp/scripts/mac/yt_fetch.sh | bash -s 'https://www.youtube.com/watch?v=…'
set -euo pipefail
URL="${1:?usage: yt_fetch.sh URL}"
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uvx >/dev/null; then
  echo "installing uv (once)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  export PATH="$HOME/.local/bin:$PATH"
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
ytdlp() { uvx --quiet --with deno --from 'yt-dlp[default]' yt-dlp --no-playlist --no-warnings "$@"; }
echo "1/4 title"
id="$(ytdlp --print id "$URL")"
ytdlp --print title "$URL" >"$id.title"
echo "2/4 subtitles"
ytdlp --skip-download --write-subs --write-auto-subs --sub-langs 'ru,uk,en,ru-orig,uk-orig,en-orig' --sub-format vtt \
  -o "$id.%(ext)s" "$URL" || echo "  no subtitles (the server will recognise the speech)"
echo "3/4 video (720p, no sound) and audio"
ytdlp -f 'bv*[height<=720][ext=mp4]/bv*[height<=720]' -o "$id.mp4" "$URL"
ytdlp -f 'bestaudio[ext=m4a]/bestaudio' -o "$id.audio.%(ext)s" "$URL"
echo "4/4 copying to the server"
ssh "tradedesk@$SERVER" 'mkdir -p ~/private/yt-inbox'
scp -q ./* "tradedesk@$SERVER:private/yt-inbox/"
echo "ok: $id ($(cat "$id.title")). Скажіть Claude: «відео $id на сервері»."

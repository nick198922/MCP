#!/usr/bin/env bash
# Append the clipboard (text copied from a Discord channel) to a local, gitignored inbox file for Claude to parse.
#   bash scripts/clip.sh NAME        -> docs/library/courses/DoubleTop-Discord/inbox/NAME.txt
# Each run adds one chunk with a separator; overlapping chunks are fine (duplicates are removed when parsing).
set -euo pipefail
name="${1:?usage: clip.sh NAME (letters, digits, - or _)}"
[[ "$name" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "bad NAME: $name" >&2; exit 1; }
dir="$(cd "$(dirname "$0")/.." && pwd)/docs/library/courses/DoubleTop-Discord/inbox"
mkdir -p "$dir"
f="$dir/$name.txt"
text="$(pbpaste)"
[[ -n "$text" ]] || { echo "clipboard is empty" >&2; exit 1; }
{ printf '\n===== chunk %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')"; printf '%s\n' "$text"; } >> "$f"
echo "+${#text} chars -> inbox/$name.txt ($(grep -c '^===== chunk' "$f") chunks)"

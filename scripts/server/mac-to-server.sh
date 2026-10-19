#!/bin/bash
# From the Mac, once: move the project files that live only on the Mac to the server, as user tradedesk.
#   - paid course materials (docs/library/courses/) and other git-ignored library files -> ~/private/library/ (mode 700),
#     symlinked as docs/library/courses in ~/tradedesk-mcp and ~/tradedesk-chat;
#   - Claude's project memory (~/.claude/projects/-Users-nick-MCP/memory) -> the server sessions' memory folder.
# Never overwrites anything on the server: a memory file that already exists there is kept and the Mac one is saved
# next to it as <name>.mac.md. Prints sizes and counts at the end. Safe to run again.
# Run from the repo folder on the Mac: bash scripts/server/mac-to-server.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
SERVER="tradedesk@${TRADEDESK_SERVER:-169.58.169.184}"
MEM="$HOME/.claude/projects/-Users-nick-MCP/memory"
export COPYFILE_DISABLE=1   # no ._ AppleDouble files in the archive

lib=()
[[ -d docs/library/courses ]] && lib+=(courses)
while IFS= read -r f; do lib+=("${f#docs/library/}"); done < <(git -c core.quotePath=false ls-files --others --ignored --exclude-standard docs/library \
  | grep -v -E '(^|/)\.DS_Store$|^docs/library/courses/')
((${#lib[@]})) || { echo "nothing to move from docs/library"; }

echo "--- library: ${lib[*]:-none}"
if ((${#lib[@]})); then
  tar --no-mac-metadata --exclude .DS_Store -C docs/library -czf - "${lib[@]}" | ssh "$SERVER" '
    set -e; umask 077; mkdir -p ~/private/library; chmod 700 ~/private
    tar -C ~/private/library -xzf -
    for clone in ~/tradedesk-mcp ~/tradedesk-chat; do
      [ -d "$clone/docs/library" ] || continue
      if [ ! -e "$clone/docs/library/courses" ]; then ln -s ~/private/library/courses "$clone/docs/library/courses"; fi
    done
    du -sh ~/private/library; ls ~/private/library'
fi

echo "--- Claude memory: $(ls "$MEM" | wc -l | tr -d ' ') files"
tar --no-mac-metadata -C "$MEM" -czf - . | ssh "$SERVER" '
  set -e; umask 077
  dst=~/.claude/projects/-home-tradedesk-tradedesk-mcp/memory; tmp=$(mktemp -d); mkdir -p "$dst"
  tar -C "$tmp" -xzf -
  for f in "$tmp"/*.md; do
    b=$(basename "$f")
    if [ "$b" = MEMORY.md ] && [ -e "$dst/MEMORY.md" ]; then
      grep -v -x -F -f "$dst/MEMORY.md" "$f" >>"$dst/MEMORY.md" || true      # append the index lines it lacks
    elif [ -e "$dst/$b" ]; then cp "$f" "$dst/${b%.md}.mac.md"; echo "kept the server copy of $b, Mac copy saved as ${b%.md}.mac.md"
    else cp "$f" "$dst/$b"; fi
  done
  rm -rf "$tmp"
  chat=~/.claude/projects/-home-tradedesk-tradedesk-chat
  [ -d "$chat/memory" ] && [ ! -L "$chat/memory" ] && rmdir "$chat/memory" 2>/dev/null || true   # an empty folder is replaced
  if [ -d ~/tradedesk-chat ] && [ ! -e "$chat/memory" ]; then mkdir -p "$chat"; ln -s "$dst" "$chat/memory"; echo "Telegram Claude now shares this memory"; fi
  [ -e "$chat/memory" ] && [ ! -L "$chat/memory" ] && echo "note: Telegram Claude has its own memory in $chat/memory (left as is)"
  echo "memory on the server: $(ls "$dst" | wc -l) files"'
echo "done"

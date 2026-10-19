#!/bin/bash
# From the Mac, once: let Telegram Claude (~/tradedesk-chat) share the owner sessions' memory. Replaces its memory folder
# only when that folder is empty; never deletes memory files.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" '
main=~/.claude/projects/-home-tradedesk-tradedesk-mcp/memory; chat=~/.claude/projects/-home-tradedesk-tradedesk-chat/memory
[ -L "$chat" ] && { echo "already linked -> $(readlink "$chat")"; exit 0; }
if [ -d "$chat" ]; then rmdir "$chat" || { echo "chat memory is not empty, left as is"; exit 1; }; fi
mkdir -p "$(dirname "$chat")"; ln -s "$main" "$chat"; echo "linked: $(ls "$chat/" | wc -l) files visible to Telegram Claude"'

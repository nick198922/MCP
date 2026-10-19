#!/bin/bash
# From the Mac: what is in the server's private files (~/private, course materials) and Claude's memory folders.
# Reads only.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" '
echo "--- ~/private"; stat -c "%A %U %n" ~/private; du -sh ~/private/library/* 2>/dev/null
echo "files: $(find ~/private -type f | wc -l)"
echo "--- links in the clones"; ls -l ~/tradedesk-mcp/docs/library/courses ~/tradedesk-chat/docs/library/courses 2>&1 | sed "s/.* -> /-> /"
echo "--- Claude memory"; for d in ~/.claude/projects/*/memory; do echo "$d: $(ls "$d" | wc -l) files$( [ -L "$d" ] && echo " (link)")"; done'

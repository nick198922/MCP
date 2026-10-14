#!/bin/bash
# From the Mac: can the server fetch YouTube transcripts (scripts/yt_transcript.py)? Prints the first lines or the reason.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" \
  "cd ~/tradedesk-mcp && ~/.local/bin/uv run -q --no-project --with 'youtube-transcript-api>=1.2,<2' python scripts/yt_transcript.py '${1:-https://youtu.be/kdEygb8UPKU}' 2>&1 | head -c 400"

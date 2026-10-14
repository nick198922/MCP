#!/bin/bash
# Step 7 of the server move: few permission prompts for the owner's Claude Code sessions in ~/tradedesk-mcp.
#   ssh tradedesk@SERVER 'bash -s' < scripts/server/07-claude-settings.sh
# Project-local, so it applies to sessions opened in ~/tradedesk-mcp (terminal or VS Code Remote-SSH) and NOT to
# the nightly agents, which run in ~/tradedesk-agents with their own narrow allow list.
set -euo pipefail
dir="$HOME/tradedesk-mcp/.claude"
mkdir -p "$dir"
[[ -f "$dir/settings.local.json" ]] && cp "$dir/settings.local.json" "$dir/settings.local.json.bak"
cat >"$dir/settings.local.json" <<'EOF'
{
  "permissions": {
    "defaultMode": "acceptEdits",
    "allow": ["Bash", "WebFetch", "WebSearch"],
    "deny": [
      "Bash(sudo:*)",
      "Bash(git push --force:*)",
      "Bash(git push -f:*)",
      "Bash(git reset --hard:*)",
      "Bash(rm -rf /:*)",
      "Bash(rm -rf ~:*)",
      "Bash(rm -rf /home:*)",
      "Read(~/.ssh/**)",
      "Read(~/.claude/.credentials.json)"
    ]
  }
}
EOF
jq . "$dir/settings.local.json" >/dev/null && echo "written $dir/settings.local.json"

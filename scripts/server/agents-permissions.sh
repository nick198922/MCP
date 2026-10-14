#!/bin/bash
# From the Mac: show every Claude Code settings source that can widen the agents' permissions on the server.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'bash -s' <<'EOF'
for f in ~/.claude/settings.json ~/.claude/settings.local.json \
         ~/tradedesk-agents/.claude/settings.json ~/tradedesk-agents/.claude/settings.local.json \
         ~/tradedesk-mcp/.claude/settings.json ~/tradedesk-mcp/.claude/settings.local.json \
         /etc/claude-code/managed-settings.json; do
  if [[ -f "$f" ]]; then echo "== $f"; jq -c '{permissions, defaultMode: .permissions.defaultMode}' "$f" 2>/dev/null || cat "$f"
  else echo "== $f (missing)"; fi
done
echo "== ~/.claude.json: per-project allowedTools"
jq -c '(.projects // {}) | map_values(.allowedTools // [])' ~/.claude.json 2>/dev/null || echo "(none)"
latest="$(ls -t ~/.claude/projects/-home-tradedesk-tradedesk-agents/*.jsonl 2>/dev/null | head -1)"
echo "== permission mode seen in the latest agent session: ${latest##*/}"
[[ -n "$latest" ]] && grep -o '"permissionMode":"[A-Za-z]*"' "$latest" | sort | uniq -c
EOF

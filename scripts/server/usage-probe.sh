#!/bin/bash
# Probe: can the server read the Claude plan usage (what /usage shows)? Prints percentages only, never the token.
#   ssh tradedesk@SERVER 'bash -s' < scripts/server/usage-probe.sh
set -euo pipefail
token="$(jq -r '.claudeAiOauth.accessToken // empty' "$HOME/.claude/.credentials.json" 2>/dev/null || true)"
if [[ -z "$token" ]]; then
  echo "no OAuth token in ~/.claude/.credentials.json"
  exit 1
fi
code="$(curl -s -m 15 -o /tmp/usage.$$ -w '%{http_code}' https://api.anthropic.com/api/oauth/usage \
  -H "Authorization: Bearer $token" -H "anthropic-beta: oauth-2025-04-20")"
echo "HTTP $code"
jq -c 'with_entries(select(.value | type == "object")) | map_values({utilization, resets_at})' /tmp/usage.$$ 2>/dev/null \
  || head -c 300 /tmp/usage.$$
rm -f /tmp/usage.$$

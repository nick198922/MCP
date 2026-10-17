#!/bin/bash
# From the Mac: state and recent journal of the project's services (or of one: services-check.sh tradedesk-dashboard),
# which collector switches are on in the MCP environment (names only, no values of secrets). Reads only.
unit="${1:-}"
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" "
for u in ${unit:-tradedesk-mcp tradedesk-agents-loop tradedesk-tgbridge tradedesk-dashboard}; do
  echo \"== \$u: \$(systemctl is-active \$u) / \$(systemctl is-enabled \$u 2>/dev/null) since \$(systemctl show \$u -p ActiveEnterTimestamp --value)\"
  journalctl -u \$u -n 8 --no-pager -o short-iso 2>&1 | tail -8
done
echo '== collector switches in /etc/tradedesk-mcp.env:'
grep -oE '^TRADEDESK_(COLLECT[A-Z_]*ENABLED|COLLECTOR_ENABLED|COLLECT_LIQUIDATIONS_VENUES)=[^ ]*' /etc/tradedesk-mcp.env 2>&1
ls -la /etc/systemd/system/tradedesk-dashboard.service 2>&1"

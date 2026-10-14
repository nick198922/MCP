#!/bin/bash
# Step 9: MCP market alerts go to the same Telegram group as the agents' messages. Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/09-alerts-telegram.sh
# Needs scripts/server/telegram-setup.sh first (it saved the bot token and the group for the agents).
# Starts with the example rules (Binance BTC funding above +30 % or below -20 % a year); more rules: MCP tools rules_*.
set -euo pipefail
SVC=tradedesk
REPO="/home/$SVC/tradedesk-mcp"
DATA=/var/lib/tradedesk-mcp
ENV=/etc/tradedesk-mcp.env
SRC="/home/$SVC/.config/tradedesk/agents.env"
RULES="$DATA/alert_rules.json"

[[ -f "$SRC" ]] || { echo "no $SRC: run scripts/server/telegram-setup.sh on the Mac first"; exit 1; }
if ! grep -q '^TELEGRAM_BOT_TOKEN=.' "$SRC" || ! grep -q '^TELEGRAM_CHAT_ID=.' "$SRC"; then
  echo "$SRC has no token or chat id: run telegram-setup.sh again"; exit 1
fi

# The code that keeps the bot token out of the journal must be running before the token is in the service.
runuser -u "$SVC" -- git -C "$REPO" pull -q --ff-only
grep -q 'def configure_logging' "$REPO/src/tradedesk/server.py"
grep -q '_BOT_TOKEN' "$REPO/src/tradedesk/core/health.py"
runuser -u "$SVC" -- "/home/$SVC/.local/bin/uv" --directory "$REPO" sync --quiet --extra dev

if [[ ! -f "$RULES" ]]; then
  install -m 640 -o "$SVC" -g "$SVC" "$REPO/src/tradedesk/data/alert_rules.example.json" "$RULES"
fi

# Rewrite the service environment without putting the token on any command line.
tmp="$(mktemp /etc/tradedesk-mcp.env.XXXXXX)"
grep -v -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|TRADEDESK_ALERTS_ENABLED|TRADEDESK_ALERTS_RULES_PATH)=' "$ENV" >"$tmp" || true
grep -E '^TELEGRAM_(BOT_TOKEN|CHAT_ID)=' "$SRC" >>"$tmp"
printf 'TRADEDESK_ALERTS_ENABLED=1\nTRADEDESK_ALERTS_RULES_PATH=%s\n' "$RULES" >>"$tmp"
chown root:"$SVC" "$tmp"
chmod 640 "$tmp"
mv "$tmp" "$ENV"

started="$(date '+%F %T')"
systemctl restart tradedesk-mcp
sleep 10
systemctl is-active tradedesk-mcp
journalctl -u tradedesk-mcp --since "$started" --no-pager | grep -E "collector started|alerts" | tail -3 || true

echo "== test message through the MCP's own Telegram channel"
runuser -u "$SVC" -- bash -c "set -a; . $ENV; set +a; cd $REPO && .venv/bin/python -" <<'EOF'
import asyncio, time
from tradedesk.alerts import build_channels
from tradedesk.alerts.engine import FiredAlert
from tradedesk.config import Settings

async def main() -> None:
    tg = [c for c in build_channels(Settings.from_env()) if c.name == "telegram"]
    if not tg:
        print("telegram channel is OFF (token or chat id missing)")
        return
    alert = FiredAlert("setup_test", int(time.time()), 0.0, 0.0, "==", {}, "tradedesk MCP: ринкові алерти підключено ✅")
    ok, err = await tg[0].send(alert)
    print("sent" if ok else f"failed: {err}")
    await tg[0]._client.aclose()

asyncio.run(main())
EOF

leaks="$(journalctl -u tradedesk-mcp --since "$started" --no-pager | grep -c '/bot[0-9]*:' || true)"
echo "bot token occurrences in the journal since restart: $leaks (must be 0)"

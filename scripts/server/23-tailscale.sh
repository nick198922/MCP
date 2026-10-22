#!/bin/bash
# Step 23: the dashboard on the owner's phone and Mac through Tailscale (owner 22.10, option A). The dashboard keeps
# listening on 127.0.0.1:8090 only; `tailscale serve` proxies https://<this machine>.<tailnet>.ts.net/ to it, reachable
# only from devices logged into the owner's tailnet (no Funnel: never on the public internet). No Tailscale SSH, no
# exit node, no subnet routes. The URL goes to /var/lib/tradedesk-mcp/assistant/dashboard_url, so the morning brief
# links its pages (scripts/assistant_brief_tg.py).
# Run through nick (interactive: it prints a login link, open it and sign in with the account the phone will use;
# on the first run it may print a second link to switch HTTPS on for the tailnet, open that one too):
#   ssh -t nick@SERVER 'sudo bash /home/tradedesk/tradedesk-mcp/scripts/server/23-tailscale.sh'
# Then install the Tailscale app on the phone (and on the Mac if wanted), log in with the same account, open the URL.
# Undo: tailscale serve reset; tailscale down (or apt-get remove tailscale); rm the URL file.
set -euo pipefail
SVC=tradedesk
URL_FILE=/var/lib/tradedesk-mcp/assistant/dashboard_url
[[ $EUID -eq 0 ]] || { echo "run with sudo (through nick)"; exit 1; }
curl -s -o /dev/null -w '' http://127.0.0.1:8090/ || { echo "the dashboard is not running on 127.0.0.1:8090 (step 15)"; exit 1; }

if ! command -v tailscale >/dev/null; then
  # The official installer adds Tailscale's signed apt repository (pkgs.tailscale.com) and installs the package.
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable --now tailscaled

if ! tailscale status >/dev/null 2>&1; then
  echo "== log in: open the link below in a browser"
  tailscale up --hostname=tradedesk --ssh=false --accept-routes=false --accept-dns=false
fi

# HTTPS on 443 of the tailnet address only -> the loopback dashboard. Telegram buttons need https.
tailscale serve --bg --https=443 http://127.0.0.1:8090
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow in on tailscale0 to any port 443 proto tcp comment 'tailscale serve: dashboard'
  ufw allow 41641/udp comment 'tailscale direct connections'
fi

dns="$(tailscale status --json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
url="https://$dns/"
install -d -o "$SVC" -g "$SVC" "$(dirname "$URL_FILE")"
printf '%s\n' "$url" >"$URL_FILE"
chown "$SVC:$SVC" "$URL_FILE"
tailscale serve status
echo "dashboard on the tailnet: $url  (radar: ${url}radar, brief: ${url}brief)"
echo "first open can take ~10 s while the certificate is issued"

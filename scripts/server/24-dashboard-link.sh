#!/bin/bash
# Step 24: a permanent link to the dashboard without the Tailscale app (owner 22.10: «постійно пропадає доступ»).
# Tailscale Funnel publishes the same https://<machine>.<tailnet>.ts.net/ address on the internet. The dashboard still
# listens on 127.0.0.1:8090 only and lets a proxied request in only with its key: once in the link (?k=…), then a
# year-long cookie (scripts/dashboard.py, access()). Before publishing, the script checks through the proxy that the
# page answers 401 without the key; otherwise it publishes nothing. Read-only page: no orders, no settings.
# Run through nick:
#   ssh -t nick@SERVER 'sudo bash /home/tradedesk/tradedesk-mcp/scripts/server/24-dashboard-link.sh'
# If Funnel is off for the tailnet, tailscale prints a link to switch it on: open it, then run the script again.
# Safe to run again any time: it switches Funnel off and on and waits until the public name answers.
# Undo (back to tailnet only): tailscale funnel --https=443 off; tailscale serve --bg --https=443 http://127.0.0.1:8090
# New key (the old link stops working): delete the key file, then systemctl restart tradedesk-dashboard; run again.
set -euo pipefail
SVC=tradedesk
KEY_FILE=/home/$SVC/.config/tradedesk/dashboard.env
[[ $EUID -eq 0 ]] || { echo "run with sudo (through nick)"; exit 1; }
command -v tailscale >/dev/null || { echo "no tailscale: run step 23 first"; exit 1; }
[[ -s $KEY_FILE ]] || { echo "no dashboard key yet ($KEY_FILE): the dashboard makes it on start; wait a minute after the update"; exit 1; }

dns="$(tailscale status --json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
ip="$(tailscale ip -4 | head -1)"
code="$(curl -s -o /dev/null -w '%{http_code}' -m 20 --resolve "$dns:443:$ip" "https://$dns/")"
if [[ "$code" != 401 ]]; then
  echo "through the proxy without the key the dashboard answers HTTP $code, not 401: not publishing"
  exit 1
fi

# Off, then on: a fresh switch makes Tailscale publish the public DNS name (on 22.10 the first switch-on left it
# unpublished for 20+ minutes).
tailscale funnel --https=443 off >/dev/null 2>&1 || true
sleep 3
tailscale funnel --bg --https=443 http://127.0.0.1:8090
tailscale funnel status
echo "waiting for the public DNS name (up to 10 min; asks ts.net's own name server)..."
pub=""
for _ in $(seq 1 40); do
  pub="$(dig +short "$dns" A @ns1.dnsimple.com 2>/dev/null | head -1)"
  [[ -n "$pub" ]] && break
  sleep 15
done
if [[ -z "$pub" ]]; then
  echo "the name is still not public. Check https://login.tailscale.com/admin/acls (nodeAttrs with \"funnel\")"
  echo "and https://login.tailscale.com/admin/dns (HTTPS Certificates on), then run this again."
else
  echo "public: $dns -> $pub; without the key: HTTP $(curl -s -o /dev/null -m 20 --resolve "$dns:443:$pub" -w '%{http_code}' "https://$dns/")"
fi
key="$(sed -n 's/^DASHBOARD_KEY=//p' "$KEY_FILE" | head -1)"
printf 'https://%s/\n' "$dns" >/var/lib/tradedesk-mcp/assistant/dashboard_url
echo
echo "the permanent link (keep it private; it opens the dashboard on any phone or computer):"
echo "  https://$dns/?k=$key"
echo "first open can take ~10 s"

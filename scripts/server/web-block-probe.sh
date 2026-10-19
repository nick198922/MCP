#!/bin/bash
# From the Mac: why a site refuses the server (IP block or a browser check?) and whether a browser is already there.
# Requests each URL once with a desktop-Chrome User-Agent and prints the status, the protection headers and the start of
# the body. Reads only. Usage: web-block-probe.sh [url ...] (default: the Bithumb notice pages).
urls=("$@")
((${#urls[@]})) || urls=("https://feed.bithumb.com/notice?category=5" "https://api.bithumb.com/v1/notices")
for u in "${urls[@]}"; do [[ "$u" =~ ^https://[A-Za-z0-9./?=_%-]+$ ]] || { echo "bad url: $u"; exit 1; }; done
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" "bash -s" -- "${urls[@]}" <<'EOF'
ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
for u in "$@"; do
  echo "=== $u"
  curl -s -m 20 -A "$ua" -H "Accept-Language: ko-KR,ko;q=0.9,en;q=0.8" -D /tmp/probe.h -o /tmp/probe.b "$u"
  head -1 /tmp/probe.h
  grep -i -E "^(server|cf-ray|cf-mitigated|x-akamai|akamai|x-cache|x-amzn|via|location):" /tmp/probe.h
  grep -o -i -E "<title>[^<]*</title>|Error 10[0-9]{2}|Sorry, you have been blocked|Just a moment" /tmp/probe.b | sort -u | head -4
  grep -q -i "<title>" /tmp/probe.b || head -c 400 /tmp/probe.b | tr -s '\n' ' '; echo
done
rm -f /tmp/probe.h /tmp/probe.b
echo "=== browser on the server"
command -v chromium chromium-browser google-chrome docker 2>/dev/null || echo "none of chromium/google-chrome/docker"
ls ~/.cache/ms-playwright 2>/dev/null || echo "no playwright browsers"
. /etc/os-release && echo "$PRETTY_NAME"
EOF

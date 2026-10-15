#!/bin/bash
# From the Mac: restart the agents loop as soon as no agent step or doctor run is working, so a running step is not
# killed. Returns at once; the waiting happens on the server. Needed only for a loop that predates its self-update.
ssh nick@"${TRADEDESK_SERVER:-169.58.169.184}" 'nohup bash -c "
  while pgrep -u tradedesk -f \"scripts/(nightly_agents|agents_doctor)\\.sh\" >/dev/null; do sleep 30; done
  sudo systemctl restart tradedesk-agents-loop
  logger -t tradedesk \"agents loop restarted while idle\"
" >/dev/null 2>&1 &
echo "the loop will restart when the current step ends"'

#!/bin/bash
# From the Mac: what the agents loop is doing right now: its last log lines, the last outcomes and the end of the
# last run log (where the reason of a failure is). The number of run-log lines is the first argument (20).
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
ssh nick@"$SERVER" 'sudo journalctl -u tradedesk-agents-loop -n 8 --no-pager -o cat'
ssh tradedesk@"$SERVER" "cd ~/.local/state/tradedesk-agents && echo '--- last outcomes' && tail -5 history.tsv \
  && last=\"\$(ls -t 20??-??-??_??????.log | head -1)\" && echo \"--- end of \$last\" && tail -${1:-20} \"\$last\""

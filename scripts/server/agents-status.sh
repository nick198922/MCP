#!/bin/bash
# From the Mac: what the agents loop is doing right now: its last log lines, the last outcomes and the end of the
# log of the last finished step (where the reason of a failure is). The number of run-log lines is the first argument.
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
ssh nick@"$SERVER" 'sudo journalctl -u tradedesk-agents-loop -n 8 --no-pager -o cat'
ssh tradedesk@"$SERVER" "cd ~/.local/state/tradedesk-agents && echo '--- last outcomes' && tail -5 history.tsv \
  && last=\"\$(tail -1 history.tsv | cut -f1).log\" && echo \"--- end of \$last\" && tail -${1:-25} \"\$last\""

#!/bin/bash
# From the Mac: set the run count of the agents' work in progress (work.state: "<branch> <runs> <first>") back to N
# (default 1), when runs were lost to a launcher failure rather than to the step itself. Shows the state before/after.
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
N="${1:-1}"
[[ "$N" =~ ^[0-9]+$ ]] || { echo "usage: $0 [runs]"; exit 1; }
ssh tradedesk@"$SERVER" "f=~/.local/state/tradedesk-agents/work.state; echo before: \$(cat \$f 2>/dev/null)
  [ -s \$f ] && read -r b r first <\$f && echo \"\$b $N \$first\" >\$f; echo after: \$(cat \$f 2>/dev/null)"

#!/bin/bash
# Which SSH key logged in as whom, from where and when: answers "is key W/o3… (tradedesk, added 2026-10-20) mine?".
# Run from the Mac:
#   ssh -t nick@169.58.169.184 'sudo bash /home/tradedesk/tradedesk-chat/scripts/server/ssh-key-usage.sh'
# Read-only. Prints fingerprints (public, not secret), users, addresses and first/last dates; no key material.
set -u
{
  journalctl -t sshd -t sshd-session --no-pager -o short-iso 2>/dev/null
  zcat -f /var/log/auth.log* 2>/dev/null
} | grep -E 'Accepted publickey' \
  | sed -E 's/^([0-9T:-]{16})[^ ]*.* Accepted publickey for ([^ ]+) from ([^ ]+) .*(SHA256:[^ ]+).*/\4 \2 \3 \1/; s/^([A-Z][a-z]{2} +[0-9]+ [0-9:]{5}).* Accepted publickey for ([^ ]+) from ([^ ]+) .*(SHA256:[^ ]+).*/\4 \2 \3 \1/' \
  | grep '^SHA256' | sort -u \
  | awk '{k=$1" "$2" "$3; n[k]++; if(!(k in first)) first[k]=$4; last[k]=$4}
         END{for(k in n) printf "%-52s login-minutes %4d  first %s  last %s\n", k, n[k], first[k], last[k]}' | sort
echo
echo "Keys allowed now:"
for h in /root /home/*; do
  f="$h/.ssh/authorized_keys"; [ -s "$f" ] && ssh-keygen -lf "$f" 2>/dev/null | awk -v f="$f" '{print "  " f ": " $2 " " $NF}'
done
echo
echo "Oldest log line searched: $( { journalctl -t sshd -t sshd-session --no-pager -o short-iso 2>/dev/null | head -1; } | cut -c1-19)"

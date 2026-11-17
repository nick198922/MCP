#!/bin/bash
# Read-only compromise check after the Contabo incident of 2026-11-14 (unauthorized access, all VNC passwords reset).
# Run from the Mac:
#   ssh -t nick@169.58.169.184 'sudo bash /home/tradedesk/tradedesk-chat/scripts/server/incident-check.sh'
# Changes nothing. Prints no secrets: for key files only dates, owners and SSH key fingerprints (not the keys).
# Optional first argument: the date to look from (default 2026-11-13, a day before the incident).
set -u
SINCE="${1:-2026-11-13}"
KNOWN_IPS="217.27.155.193 78.137.2.92 37.73.11.170"   # the owner's addresses seen in wtmp before the incident

hr() { echo; echo "== $*"; }

hr "machine"
hostname; uptime
echo "last boot: $(who -b | awk '{print $3, $4}')"

hr "reboots / shutdowns since $SINCE (a console attacker needs a reboot for single-user mode)"
last -x -F reboot shutdown --since "$SINCE" 2>/dev/null | grep -v '^wtmp' | sed '/^$/d' || true

hr "ssh logins accepted since $SINCE (user, method, ip)"
journalctl -q --since "$SINCE" -t sshd -t sshd-session --no-pager 2>/dev/null \
  | grep -E 'Accepted ' | sed -E 's/.*Accepted ([a-z-]+) for ([^ ]+) from ([^ ]+).*/\2 \1 \3/' \
  | sort | uniq -c | sort -rn
echo "-- logins from addresses NOT in the known list:"
journalctl -q --since "$SINCE" -t sshd -t sshd-session --no-pager 2>/dev/null | grep -E 'Accepted ' \
  | grep -vE "from ($(echo "$KNOWN_IPS" | sed 's/ /|/g;s/\./\\./g')) " | head -20 || true
echo "(Tailscale / 100.x addresses are your own tailnet)"

hr "console (tty / VNC) logins since $SINCE"
journalctl -q --since "$SINCE" --no-pager _COMM=login 2>/dev/null | grep -iE 'session opened|LOGIN|FAILED' | head -20
last -F --since "$SINCE" 2>/dev/null | grep -E 'tty|console' || echo "none in wtmp"

hr "failed ssh attempts since $SINCE (count only)"
journalctl -q --since "$SINCE" -t sshd -t sshd-session --no-pager 2>/dev/null | grep -cE 'Failed|Invalid user' || true

hr "sshd effective settings"
sshd -T 2>/dev/null | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication|port) '

hr "accounts"
echo "uid 0: $(awk -F: '$3==0{print $1}' /etc/passwd | tr '\n' ' ')"
echo "login shells: $(awk -F: '$7 ~ /sh$/ {print $1}' /etc/passwd | tr '\n' ' ')"
echo "sudo group: $(getent group sudo | cut -d: -f4)"
stat -c '%y  %n' /etc/passwd /etc/shadow /etc/group /etc/sudoers /etc/sudoers.d/* 2>/dev/null

hr "authorized_keys (date, count, fingerprints: compare with 'ssh-keygen -lf ~/.ssh/id_*.pub' on the Mac)"
for h in /root /home/*; do
  f="$h/.ssh/authorized_keys"
  [ -f "$f" ] || continue
  echo "$f  modified $(stat -c '%y' "$f" | cut -d. -f1)  keys $(grep -cvE '^\s*(#|$)' "$f")"
  ssh-keygen -lf "$f" 2>/dev/null | awk '{print "   ", $2, $3, $4}'
done

hr "files changed since $SINCE in system places (first 60)"
find /etc /root /usr/local /usr/lib/systemd/system /etc/systemd /var/spool/cron /home/nick \
  -xdev -newermt "$SINCE" -type f 2>/dev/null \
  | grep -vE '^/etc/(ld\.so\.cache|mtab|resolv\.conf|apt/|machine-id)|\.cache/|/var/spool/cron/crontabs/tradedesk$' \
  | head -60

hr "cron (root and system)"
crontab -l -u root 2>/dev/null | grep -vE '^\s*(#|$)' || echo "root: none"
ls -la --time-style=+%F /etc/cron.d 2>/dev/null | tail -n +2

hr "listening ports (address, process)"
ss -tlnpH 2>/dev/null | awk '{print $4, $6}' | sed -E 's/users:\(\("([^"]+)".*/\1/' | sort -u

hr "secret files: owner, mode, modified (contents not read)"
stat -c '%U:%G %a %y  %n' /etc/tradedesk*.env /home/tradedesk/.config/tradedesk/* 2>/dev/null | cut -c1-200

hr "firewall"
ufw status 2>/dev/null | head -15 || echo "ufw not present"

echo
echo "Done. Send the whole output to Claude (no secrets in it)."

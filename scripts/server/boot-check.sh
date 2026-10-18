#!/bin/bash
# From the Mac: why and when the server last rebooted (read-only): reboot history, unattended-upgrades log tail,
# whether a reboot was required, and the last lines the previous boot left in the journal (if readable).
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" '
echo "--- uptime"; uptime
echo "--- reboots (last -x)"; last -x reboot shutdown 2>/dev/null | head -8
echo "--- reboot-required"; ls -l /var/run/reboot-required 2>/dev/null || echo "none"
echo "--- unattended-upgrades"; grep -h -iE "reboot|Packages that will be upgraded|All upgrades installed" /var/log/unattended-upgrades/unattended-upgrades.log 2>/dev/null | tail -6 || echo "log not readable"
grep -h "Automatic-Reboot" /etc/apt/apt.conf.d/* 2>/dev/null | grep -v "^//" | head -3
echo "--- previous boot, last journal lines"; journalctl -b -1 -n 15 --no-pager 2>/dev/null || echo "journal not readable for this user"
'

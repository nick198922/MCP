#!/bin/bash
# From the Mac: the agents loop's systemd settings (usage threshold, memory caps, OOM policy, drop-ins), when the loop
# last started, and whether a step is running now. Reads only.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" '
systemctl show tradedesk-agents-loop.service -p Environment -p MemoryMax -p OOMPolicy -p ActiveState -p ActiveEnterTimestamp -p DropInPaths
echo "--- usage.conf:"; cat /etc/systemd/system/tradedesk-agents-loop.service.d/usage.conf 2>/dev/null || echo "(none)"
echo "--- loop start lines:"; grep -h "agents loop started" ~/.local/state/tradedesk-agents/loop.log 2>/dev/null | tail -2
pgrep -u tradedesk -f "scripts/(nightly_agents|agents_doctor)\.sh" >/dev/null && echo "step running now: yes" || echo "step running now: no"'

#!/bin/bash
# Step 6 of the server move: no SSH login as root; administer as nick (sudo). Run THROUGH nick, which proves
# nick + sudo work before root is shut out:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/06-no-root.sh
set -euo pipefail
sed -i 's/^PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config.d/01-tradedesk.conf
sshd -t
systemctl try-reload-or-restart ssh
sshd -T | grep -E '^(permitrootlogin|passwordauthentication) '
echo "root SSH login disabled; administer with: ssh nick@<server>, then sudo"

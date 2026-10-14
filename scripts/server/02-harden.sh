#!/bin/bash
# Step 2 of the server move: packages, users, key-only SSH, firewall, automatic security updates. Run as root:
#   ssh root@SERVER 'bash -s' < scripts/server/02-harden.sh
# Users: nick (admin, sudo) and tradedesk (services and agents, no sudo). Both get root's authorized key.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
ADMIN=nick
SVC=tradedesk

echo "== packages"
apt-get update -q
apt-get -y -q -o Dpkg::Options::=--force-confold upgrade
apt-get -y -q install git curl ufw unattended-upgrades fail2ban sqlite3 jq make

echo "== time zone"
timedatectl set-timezone Europe/Kyiv

echo "== users"
for u in "$ADMIN" "$SVC"; do
  id "$u" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$u"
  install -d -m 700 -o "$u" -g "$u" "/home/$u/.ssh"
  install -m 600 -o "$u" -g "$u" /root/.ssh/authorized_keys "/home/$u/.ssh/authorized_keys"
  test -s "/home/$u/.ssh/authorized_keys"
done
usermod -aG sudo "$ADMIN"
echo "$ADMIN ALL=(ALL) NOPASSWD:ALL" >"/etc/sudoers.d/90-$ADMIN"
chmod 440 "/etc/sudoers.d/90-$ADMIN"
visudo -cf "/etc/sudoers.d/90-$ADMIN"

echo "== ssh: keys only"
# sshd keeps the FIRST value it reads, and sshd_config.d files load in name order (cloud-init ships
# 50-cloud-init.conf with PasswordAuthentication yes), so this file must sort first.
cat >/etc/ssh/sshd_config.d/01-tradedesk.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sshd -t
systemctl try-reload-or-restart ssh
sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '

echo "== firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
ufw status | head -6

echo "== automatic security updates, fail2ban"
dpkg-reconfigure -f noninteractive unattended-upgrades
systemctl enable --now unattended-upgrades fail2ban
echo "== done"

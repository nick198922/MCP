#!/bin/bash
# One-time key for the off-server backup (scripts/offsite_backup.py): makes an age key pair, keeps only the public key
# on the server (~/.config/tradedesk/backup.pub) and prints the secret key ONCE for the owner's password manager; the
# secret is never written to disk here. Without it the backups in Telegram cannot be opened. Run as tradedesk:
#   bash ~/tradedesk-mcp/scripts/backup_key.sh
set -euo pipefail
PUB="$HOME/.config/tradedesk/backup.pub"
KEYGEN="$HOME/.local/bin/age-keygen"
[[ -x "$KEYGEN" ]] || { echo "no $KEYGEN"; exit 1; }
if [[ -s "$PUB" && "${1:-}" != --new ]]; then echo "key already set ($PUB); --new replaces it (old backups keep the old key)"; exit 1; fi
secret="$("$KEYGEN" 2>/dev/null)"
umask 077
mkdir -p "$(dirname "$PUB")"
"$KEYGEN" -y <<<"$secret" >"$PUB"
echo "Збережіть цей секретний ключ у менеджері паролів (запис «tradedesk backup age»). Більше він ніде не показується:"
echo
grep '^AGE-SECRET-KEY-' <<<"$secret"
echo
echo "Публічний ключ записано в $PUB."

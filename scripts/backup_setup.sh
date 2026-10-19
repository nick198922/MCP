#!/bin/bash
# One-time setup of the restic backup (scripts/backup.sh) for an S3-compatible bucket (Cloudflare R2, Backblaze B2).
# Asks for the bucket endpoint and keys, makes a random repository password, prints it ONCE for the owner's password
# manager (without it the backup cannot be read), writes ~/.config/tradedesk/restic.env (0600), creates the
# repository and takes the first snapshot. Run as tradedesk in the owner's own terminal (not in a Claude chat):
#   bash ~/tradedesk-mcp/scripts/backup_setup.sh
set -euo pipefail
CONF="$HOME/.config/tradedesk"
ENV="$CONF/restic.env"
PASS="$CONF/restic.pass"
RESTIC="$HOME/.local/bin/restic"
[[ -x "$RESTIC" ]] || { echo "no $RESTIC"; exit 1; }
[[ -s "$ENV" ]] && { echo "already set up ($ENV); delete it to start over"; exit 1; }
umask 077
mkdir -p "$CONF"
echo "Сховище S3 (Cloudflare R2: https://<ACCOUNT_ID>.r2.cloudflarestorage.com; Backblaze B2: https://s3.<регіон>.backblazeb2.com)"
read -rp "Endpoint (https://...): " endpoint
read -rp "Назва бакета: " bucket
read -rp "Access Key ID: " key_id
read -rsp "Secret Access Key (не показується): " secret
echo
[[ "$endpoint" == http* ]] || endpoint="https://$endpoint"
if [[ "$endpoint" == *backblazeb2.com* && ${#key_id} -lt 20 ]]; then
  echo "Це схоже на головний ключ акаунта Backblaze (keyID ${#key_id} символів); для S3 потрібен ключ застосунку"
  echo "(Application Keys → Add a New Application Key, доступ лише до бакета, Read and Write; keyID ≈ 25 символів)."
  exit 1
fi
head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40 >"$PASS"
cat >"$ENV" <<EOF
RESTIC_REPOSITORY=s3:${endpoint%/}/$bucket/tradedesk
RESTIC_PASSWORD_FILE=$PASS
AWS_ACCESS_KEY_ID=$key_id
AWS_SECRET_ACCESS_KEY=$secret
AWS_DEFAULT_REGION=auto
EOF
set -a
source "$ENV"
set +a
"$RESTIC" init || { rm -f "$ENV"; echo "не вдалося створити сховище; перевірте дані й запустіть знову"; exit 1; }
echo
echo "Пароль бекапу — збережіть у менеджері паролів (запис «tradedesk backup restic»), разом з endpoint і бакетом:"
echo
cat "$PASS"
echo
echo
echo "Перший знімок (кілька хвилин)..."
bash "$(dirname "$0")/backup.sh" hourly
bash "$(dirname "$0")/backup.sh" status

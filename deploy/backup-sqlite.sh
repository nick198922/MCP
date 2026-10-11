#!/usr/bin/env bash
# SQLite online backup for tradedesk-mcp. Uses `sqlite3 .backup` so the file
# can be copied safely while the collector keeps writing (WAL stays consistent).
#
# Usage:
#   ./deploy/backup-sqlite.sh [SRC] [DEST_DIR]
#
# Defaults: SRC=/data/tradedesk.sqlite, DEST_DIR=/data/backups.
# A YYYY-MM-DD timestamp is appended to the filename; the script keeps the last
# BACKUP_KEEP_DAYS files (default 30).

set -euo pipefail

SRC="${1:-/data/tradedesk.sqlite}"
DEST_DIR="${2:-/data/backups}"
KEEP="${BACKUP_KEEP_DAYS:-30}"

if [ ! -f "$SRC" ]; then
  echo "backup: source $SRC not found" >&2
  exit 2
fi

mkdir -p "$DEST_DIR"
STAMP="$(date -u +%Y-%m-%dT%H%M%SZ)"
OUT="$DEST_DIR/tradedesk-$STAMP.sqlite"

# sqlite3 .backup takes an exclusive-while-copying lock but keeps writers unblocked under WAL.
sqlite3 "$SRC" ".backup '$OUT'"
gzip -9 "$OUT"
echo "backup: wrote $OUT.gz"

# Prune older gzipped backups.
find "$DEST_DIR" -maxdepth 1 -type f -name 'tradedesk-*.sqlite.gz' -mtime "+$KEEP" -print -delete || true

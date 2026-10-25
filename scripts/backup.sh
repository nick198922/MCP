#!/bin/bash
# Off-server backup with restic (docs/AGENTS.md → «Бекап поза сервером»): encrypted, deduplicated, incremental.
# Settings: ~/.config/tradedesk/restic.env (0600, written by scripts/backup_setup.sh): RESTIC_REPOSITORY,
# RESTIC_PASSWORD_FILE and the storage keys. Not configured yet → prints so and exits 0 (timers stay quiet).
#   bash scripts/backup.sh hourly    # snapshot (user timer, every hour)
#   bash scripts/backup.sh daily     # snapshot + retention + integrity check; Sundays also a restore test and a
#                                    # one-line summary in Telegram
#   bash scripts/backup.sh status    # the latest snapshots and the repository size
# Failures go to the Telegram status topic, the same failure once a day (it repeats every hour until fixed: 21-25.10
# sent ~100 identical messages), and the first success after a failure says so. No Claude tokens are used.
set -euo pipefail
MODE="${1:-hourly}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV="$HOME/.config/tradedesk/restic.env"
RESTIC="$HOME/.local/bin/restic"
DATA=/var/lib/tradedesk-mcp
STAGE="$HOME/.cache/tradedesk-backup"
[[ -s "$ENV" && -x "$RESTIC" ]] || { echo "backup not configured (run scripts/backup_setup.sh)"; exit 0; }
set -a
source "$ENV"
set +a
export RESTIC_CACHE_DIR="$HOME/.cache/restic"

FAILED="${XDG_STATE_HOME:-$HOME/.local/state}/tradedesk-backup.failed"  # "<day> <reason>" of the last told failure
fail() {
  echo "backup $MODE failed: $1" >&2
  local key
  key="$(date +%F) $1"
  if [[ "$(cat "$FAILED" 2>/dev/null)" != "$key" ]]; then
    python3 "$REPO/scripts/tg_send.py" --topic status \
      "🛑 Бекап ($MODE) не вдався: $1. Повторюватиметься щогодини; наступне повідомлення — завтра або коли причина зміниться." \
      >/dev/null 2>&1 || true
    mkdir -p "$(dirname "$FAILED")" && echo "$key" >"$FAILED"
  fi
  exit 1
}
recovered() {
  [[ -e "$FAILED" ]] || return 0
  rm -f "$FAILED"
  python3 "$REPO/scripts/tg_send.py" --silent --topic status "✅ Бекап ($MODE) знову працює." >/dev/null 2>&1 || true
}

snapshot() {
  mkdir -p "$STAGE"
  # A consistent copy of the live database (WAL-safe), kept uncompressed so restic stores only the changed chunks.
  sqlite3 "$DATA/tradedesk.sqlite" ".backup '$STAGE/tradedesk.sqlite'" || fail "sqlite backup"
  local paths=("$STAGE/tradedesk.sqlite" "$HOME/.local/state/tradedesk-agents")
  local p
  for p in "$HOME"/.claude/projects/*/memory "$DATA/journal" "$DATA/assistant" "$DATA/macro" "$DATA/alert_rules.json" \
    "$HOME/private"; do
    [[ -e "$p" ]] && paths+=("$p")
  done
  # Raw video and audio (~/private/yt-inbox, ~9 GB) can be fetched again; transcripts and frames next to them stay.
  local rc=0
  "$RESTIC" backup --quiet --tag "$MODE" --exclude-caches \
    --exclude '*.mp4' --exclude '*.m4a' --exclude '*.webm' --exclude '*.mkv' --exclude '*.mov' \
    "${paths[@]}" 2>"$STAGE/restic.err" || rc=$?
  cat "$STAGE/restic.err" >&2
  ((rc == 0)) || fail "$(why)"
  recovered
}

# The reason of a failed restic run in a few words (the storage cap is the one seen in practice).
why() {
  if grep -q "storage cap exceeded" "$STAGE/restic.err" 2>/dev/null; then
    echo "сховище Backblaze переповнене (storage cap exceeded)"
  else
    echo "restic backup"
  fi
}
case "$MODE" in
  hourly)
    snapshot
    ;;
  daily)
    snapshot
    "$RESTIC" forget --quiet --prune --keep-hourly 48 --keep-daily 14 --keep-weekly 8 --keep-monthly 12 \
      || fail "restic forget/prune"
    "$RESTIC" check --quiet --read-data-subset 2% || fail "restic check"
    if [[ "$(date +%u)" == 7 ]]; then
      t="$(mktemp -d)"
      trap 'rm -rf "$t"' EXIT
      "$RESTIC" restore latest --quiet --target "$t" --include "$STAGE/tradedesk.sqlite" || fail "restore test"
      [[ "$(sqlite3 "$t$STAGE/tradedesk.sqlite" 'PRAGMA quick_check;')" == ok ]] || fail "restored database is damaged"
      size="$("$RESTIC" stats --quiet --mode raw-data --json | python3 -c 'import json,sys; print(round(json.load(sys.stdin)["total_size"]/1e6))')"
      n="$("$RESTIC" snapshots --quiet --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
      python3 "$REPO/scripts/tg_send.py" --silent --topic status \
        "✅ Бекап: тест відновлення бази пройшов; знімків $n, у сховищі $size МБ" >/dev/null 2>&1 || true
    fi
    ;;
  status)
    "$RESTIC" snapshots --compact | tail -6
    "$RESTIC" stats --mode raw-data | grep -E 'Total Size|Snapshots'
    ;;
  *)
    echo "usage: $0 hourly|daily|status" >&2
    exit 2
    ;;
esac

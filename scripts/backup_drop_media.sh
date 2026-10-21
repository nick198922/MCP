#!/usr/bin/env bash
# One-off (2026-10-21): remove raw video/audio from every existing restic snapshot and prune, after ~9 GB of
# ~/private/yt-inbox media filled the bucket ("storage cap exceeded"). backup.sh excludes these files from now on.
# restic needs to upload a lock and new indexes, so raise the bucket cap first if uploads are refused.
set -euo pipefail
ENV="$HOME/.config/tradedesk/restic.env"
RESTIC="$HOME/.local/bin/restic"
set -a
source "$ENV"
set +a
export RESTIC_CACHE_DIR="$HOME/.cache/restic"
"$RESTIC" rewrite --forget \
  --exclude '*.mp4' --exclude '*.m4a' --exclude '*.webm' --exclude '*.mkv' --exclude '*.mov'
"$RESTIC" prune
"$RESTIC" stats --mode raw-data | tail -3

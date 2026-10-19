#!/bin/bash
# Install the user timers kept in git (deploy/user-units/*.service, *.timer) for the service user, without root:
# links them into ~/.config/systemd/user, enables and starts every timer, and disables and removes the links of units
# deleted from git. Run by tradedesk-update after each pull of main (scripts/server/20-self-service.sh), as tradedesk;
# needs linger for tradedesk (same script). Units run unprivileged as tradedesk, like the MCP service and the agents.
#   bash scripts/user_units_sync.sh            # sync
#   bash scripts/user_units_sync.sh --check    # print what would change
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO/deploy/user-units"
DST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
CHECK=0
[[ "${1:-}" == --check ]] && CHECK=1
sc() { if ((CHECK)); then echo "would: systemctl --user $*"; else systemctl --user "$@"; fi; }

mkdir -p "$DST"
changed=0
# Units removed from git: stop, disable and unlink.
for link in "$DST"/*.service "$DST"/*.timer; do
  [[ -L "$link" ]] || continue
  target="$(readlink "$link")"
  [[ "$target" == "$SRC"/* ]] || continue
  [[ -e "$target" ]] && continue
  name="$(basename "$link")"
  [[ "$name" == *.timer ]] && sc disable --now "$name" || true
  ((CHECK)) && echo "would: remove $link" || rm -f "$link"
  changed=1
done
# Units in git: link the new ones.
shopt -s nullglob
for unit in "$SRC"/*.service "$SRC"/*.timer; do
  name="$(basename "$unit")"
  [[ "$(readlink "$DST/$name" 2>/dev/null || true)" == "$unit" ]] && continue
  ((CHECK)) && echo "would: link $name" || ln -sfn "$unit" "$DST/$name"
  changed=1
done
# Edits of linked files also need a reload, so reload on every run (cheap).
sc daemon-reload
for unit in "$SRC"/*.timer; do
  name="$(basename "$unit")"
  systemctl --user is-enabled -q "$name" 2>/dev/null && systemctl --user is-active -q "$name" 2>/dev/null && continue
  sc enable --now "$name"
  changed=1
done
((changed)) && echo "user units: synced" || echo "user units: no changes"
systemctl --user list-timers --no-pager 2>/dev/null | grep -E '\.timer' || true

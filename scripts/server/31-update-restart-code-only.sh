#!/bin/bash
# Step 31: the hourly tradedesk-update (step 20) restarts tradedesk-mcp only when something the service reads changed.
# Until now any merge to main, docs-only too, restarted the MCP server and its 24/7 collector, and each restart leaves
# gaps in the WebSocket liquidations and order-book walls that cannot be fetched later (item 469 of
# docs/ideas/IDEA-20261102-standing-improvements.md).
#   - unchanged: pull main as tradedesk, user timers sync (scripts/user_units_sync.sh) on every run, uv sync + restart;
#     `tradedesk-update --force` always restarts, so the sudoers rule of step 21 keeps working as it is;
#   - new: it compares HEAD with the commit the service was last restarted with, kept in the root-owned stamp
#     /var/lib/tradedesk-update/deployed (written only after a successful uv sync + restart), so a pull of the clone by
#     another step or a failed uv sync is no longer lost; the decision is scripts/restart_needed.sh, run as tradedesk
#     (root never runs repository code). Root restarts on anything but its exact answer "skip": no stamp, an error,
#     a missing or crashing helper all mean restart. A merge (the pull moved HEAD) also restarts a service that is not
#     active, as any merge did before; an unchanged main leaves a service stopped by hand alone, as before.
# Ends with one `tradedesk-update --force` (writes the first stamp, restarts the MCP once).
# Undo: re-run step 20 (writes the old tradedesk-update back); the stamp directory can stay or go. Any later re-run of
# step 20 undoes this step too: run step 31 again after it.
# Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/31-update-restart-code-only.sh
set -euo pipefail
SVC=tradedesk
REPO="/home/$SVC/tradedesk-mcp"
DST=/usr/local/sbin/tradedesk-update
test -x "$DST" || { echo "run step 20 first"; exit 1; }
runuser -u "$SVC" -- git -C "$REPO" pull -q --ff-only
# Each check exits on its own: a failed non-final `a && b` does not stop a `set -e` script. Without the helper the new
# update script would restart every hour, so nothing is installed until main has it (tests/test_restart_needed.py).
for f in scripts/restart_needed.sh scripts/user_units_sync.sh; do
  test -f "$REPO/$f" || { echo "no $f in $REPO: merge the PR that adds it to main first"; exit 1; }
done

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
# Quoted heredoc: the update script is written literally. tests/test_restart_needed.py runs this same text with stubs.
cat >"$tmp" <<'EOF'
#!/bin/bash
# tradedesk-update, written by scripts/server/31-update-restart-code-only.sh (step 31). Root only restarts; every
# repository command runs as tradedesk. `--force` restarts always.
set -euo pipefail
SVC=tradedesk
REPO=/home/tradedesk/tradedesk-mcp
UV=/home/tradedesk/.local/bin/uv
RUNUSER=/usr/sbin/runuser
STAMP_DIR=/var/lib/tradedesk-update
STAMP="$STAMP_DIR/deployed"
as() { "$RUNUSER" -u "$SVC" -- "$@"; }
prev="$(as git -C "$REPO" rev-parse HEAD)" || prev=none
[[ "$prev" =~ ^[0-9a-f]{40}$ ]] || prev=none
as git -C "$REPO" pull -q --ff-only
new="$(as git -C "$REPO" rev-parse HEAD)"
[[ "$new" =~ ^[0-9a-f]{40}$ ]] || { echo "unexpected HEAD from git"; exit 1; }
as env XDG_RUNTIME_DIR="/run/user/$(id -u "$SVC")" bash "$REPO/scripts/user_units_sync.sh" || echo "user units sync failed"
old=none
if [[ -f "$STAMP" && ! -L "$STAMP" ]]; then
  read -r old <"$STAMP" || true
  [[ "$old" =~ ^[0-9a-f]{40}$ ]] || old=none
fi
if [[ "${1:-}" != --force ]]; then
  # A stopped service is started again by a merge, as before step 31 (any merge restarted it); while main does not
  # move, a service stopped by hand stays stopped.
  if [[ "$new" != "$prev" ]] && ! systemctl is-active --quiet tradedesk-mcp.service; then
    ans="restart: service not active and main moved"
  else
    # The helper's output is only compared with the literal "skip"; it is never sourced or evaluated.
    ans="$(timeout -k 5 120 "$RUNUSER" -u "$SVC" -- bash "$REPO/scripts/restart_needed.sh" "$REPO" "$old" </dev/null)" || ans="helper failed"
  fi
  if [[ "$ans" == skip ]]; then
    echo "no restart: nothing the service reads changed since ${old:0:7} (main ${new:0:7})"
    exit 0
  fi
  echo "restart needed: ${ans:0:200}"
fi
as "$UV" --directory "$REPO" sync --quiet --extra dev
systemctl restart tradedesk-mcp.service
install -d -m 755 "$STAMP_DIR"
printf '%s\n' "$new" >"$STAMP.tmp"
mv -f "$STAMP.tmp" "$STAMP"
echo "updated ${old:0:7} -> ${new:0:7}, tradedesk-mcp restarted"
EOF
bash -n "$tmp"
if cmp -s "$tmp" "$DST"; then
  echo "tradedesk-update already up to date"
else
  install -m 755 -o root -g root "$tmp" "$DST"
  echo "tradedesk-update rewritten"
fi
"$DST" --force
echo "stamp: $(cat /var/lib/tradedesk-update/deployed)"
systemctl list-timers tradedesk-update.timer --no-pager | head -3

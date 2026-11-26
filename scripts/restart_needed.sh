#!/bin/bash
# Does tradedesk-mcp (MCP server + 24/7 collector) need a restart to run what is checked out at HEAD?
# Item 469 of docs/ideas/IDEA-20261102-standing-improvements.md: a restart leaves gaps in the WebSocket liquidations and
# the order-book wall events that cannot be fetched later, so docs-only merges should not restart the service.
#   bash scripts/restart_needed.sh REPO DEPLOYED_SHA
# DEPLOYED_SHA is the full commit the service was last restarted with (the root-owned stamp of tradedesk-update,
# scripts/server/31-update-restart-code-only.sh). Run as tradedesk, never as root; only git, no network, no writes.
#
# Prints exactly one line on stdout: "skip" when the trees of DEPLOYED_SHA and HEAD differ only in paths the running
# service never reads (SKIP_DIRS / SKIP_FILES below), else "restart: <reason>". "skip" is printed only as the last
# step after every check passed, so a crash anywhere prints no "skip"; the caller restarts on anything but the exact
# line "skip" (no output, an error, a missing script).
#
# Why these paths are safe (checked against src/ on 2026-11-26): the service reads from the repository only
# src/tradedesk/** (code and package data), deploy/features.env (src/tradedesk/features.py) and
# deploy/coingecko_extra_ids.txt (collector/scheduler.py), plus pyproject.toml / uv.lock through `uv sync`. Nothing in
# src/ reads docs/, tests/, .claude/, .github/ or the top-level markdown; nothing in the service imports or runs
# scripts/ (only execution/demo_trader.py imports scripts/tg_send.py, and that runs as its own user timer, a fresh
# process each time); deploy/user-units/ is read only by scripts/user_units_sync.sh, which tradedesk-update runs on
# every pull anyway. A path not on the list (also a new top-level file) means restart.
set -uo pipefail

SKIP_DIRS=(docs/ tests/ scripts/ .claude/ .github/ deploy/user-units/)
SKIP_FILES=(README.md CHANGELOG.md CLAUDE.md)

restart() {
  echo "restart: $*"
  exit 0
}

[[ $# -eq 2 ]] || restart "usage: restart_needed.sh REPO DEPLOYED_SHA"
repo="$1"
deployed="$2"
[[ "$deployed" =~ ^[0-9a-f]{40}$ ]] || restart "no valid deployed commit"
[[ -d "$repo" ]] || restart "no repository"

# A caller's environment must not point git at another repository or index.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_NAMESPACE
g() { git -c core.quotePath=true --no-pager -C "$repo" "$@"; }

head="$(g rev-parse --verify --quiet 'HEAD^{commit}')" || restart "cannot read HEAD"
[[ "$head" =~ ^[0-9a-f]{40}$ ]] || restart "unexpected HEAD"
known="$(g rev-parse --verify --quiet "${deployed}^{commit}")" || restart "deployed commit ${deployed:0:7} unknown"
[[ "$known" == "$deployed" ]] || restart "deployed commit ${deployed:0:7} unknown"

t_old="$(g rev-parse --verify --quiet "${deployed}^{tree}")" || restart "cannot read the deployed tree"
t_new="$(g rev-parse --verify --quiet "${head}^{tree}")" || restart "cannot read the HEAD tree"
[[ "$t_old" =~ ^[0-9a-f]{40}$ && "$t_new" =~ ^[0-9a-f]{40}$ ]] || restart "unexpected tree id"
if [[ "$t_old" == "$t_new" ]]; then
  echo "restart_needed: same tree as ${deployed:0:7} (HEAD ${head:0:7})" >&2
  echo skip
  exit 0
fi

# Plumbing (no diff.external, no rename config); --no-renames lists both sides of a move.
changed="$(g diff-tree -r --name-only --no-renames "$deployed" "$head")" || restart "git diff-tree failed"
[[ -n "$changed" ]] || restart "trees differ but no changed paths listed"

n=0
while IFS= read -r path; do
  # Quoted (non-ASCII, control characters), spaced or otherwise odd names are not matched: restart.
  [[ "$path" =~ ^[A-Za-z0-9._/@+-]+$ ]] || restart "unusual path name"
  ok=0
  for d in "${SKIP_DIRS[@]}"; do [[ "$path" == "$d"* && "$path" != "$d" ]] && ok=1; done
  for f in "${SKIP_FILES[@]}"; do [[ "$path" == "$f" ]] && ok=1; done
  ((ok)) || restart "$path changed"
  n=$((n + 1))
done <<<"$changed"
((n > 0)) || restart "no changed paths read"

echo "restart_needed: $n changed paths since ${deployed:0:7}, none read by the service (HEAD ${head:0:7})" >&2
echo skip

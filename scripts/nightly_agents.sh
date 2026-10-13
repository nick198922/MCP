#!/bin/bash
# One nightly pipeline step by the project agents, in a separate git worktree; the result is an agent/* branch.
# Usage: scripts/nightly_agents.sh [--dry-run]
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WT="${TRADEDESK_AGENTS_WORKTREE:-$(dirname "$REPO")/tradedesk-agents}"
LOG_DIR="$HOME/Library/Logs/tradedesk-agents"
STAMP="$(date +%Y-%m-%d_%H%M)"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/$STAMP.log" 2>&1
LOCK="$LOG_DIR/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another run holds $LOCK; exiting"
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

echo "== $(date) start (dry=$DRY)"
git -C "$REPO" fetch --quiet origin
if [[ ! -d "$WT" ]]; then
  git -C "$REPO" worktree add --detach "$WT" origin/main
fi
cd "$WT"
# Leftovers from an interrupted run are stashed, not deleted.
if [[ -n "$(git status --porcelain)" ]]; then
  git stash push --include-untracked -m "nightly-leftover-$STAMP"
fi
git checkout --quiet --detach origin/main
uv sync --quiet --extra dev
echo "worktree $WT at $(git rev-parse --short HEAD)"

PROMPT="$(cat .claude/nightly.md)"
if [[ $DRY -eq 1 ]]; then
  PROMPT="$PROMPT

DRY RUN: do steps 0-2 only (read and decide). Make no file changes, no commits, no pushes, and do not start subagents. Print the plan you would execute."
fi

ALLOWED=(
  Read Write Edit Glob Grep WebSearch WebFetch Agent
  "Bash(uv run:*)" "Bash(uv sync:*)" "Bash(make:*)"
  "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git show:*)" "Bash(git branch:*)"
  "Bash(git fetch:*)" "Bash(git rev-parse:*)" "Bash(git switch -c agent/:*)" "Bash(git add:*)" "Bash(git commit:*)"
  "Bash(git push -u origin agent/:*)" "Bash(gh pr:*)" "Bash(ls:*)" "Bash(date:*)" "Bash(which:*)"
)
DENIED=(
  "Bash(git push origin main:*)" "Bash(git push --force:*)" "Bash(git push -f:*)" "Bash(git reset --hard:*)"
  "Bash(git config:*)" "Bash(rm -rf:*)"
)

caffeinate -i claude -p "$PROMPT" --permission-mode dontAsk \
  --allowedTools "${ALLOWED[@]}" --disallowedTools "${DENIED[@]}" | tee "$LOG_DIR/$STAMP.summary.md"

echo "== $(date) done"
osascript -e "display notification \"Підсумок: $LOG_DIR/$STAMP.summary.md\" with title \"tradedesk: нічний прогін агентів\"" || true

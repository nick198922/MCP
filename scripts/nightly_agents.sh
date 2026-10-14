#!/bin/bash
# One nightly pipeline step by the project agents, in a separate git worktree; the result is an agent/* branch.
# Usage: scripts/nightly_agents.sh [--dry-run]
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WT="${TRADEDESK_AGENTS_WORKTREE:-$(dirname "$REPO")/tradedesk-agents}"
if [[ "$(uname)" == "Darwin" ]]; then
  LOG_DIR="$HOME/Library/Logs/tradedesk-agents"
else
  LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/tradedesk-agents"
fi
# Optional secrets for the morning notification (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID); keep the file 0600.
[[ -f "$HOME/.config/tradedesk/agents.env" ]] && . "$HOME/.config/tradedesk/agents.env"
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
rm -f .agent/branch .agent/commit-msg.txt
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
  "Bash(git fetch:*)" "Bash(git rev-parse:*)" "Bash(git ls-tree:*)" "Bash(git add:*)"
  "Bash(ls:*)" "Bash(date:*)" "Bash(which:*)" "Bash(head:*)" "Bash(tail:*)" "Bash(wc:*)" "Bash(mkdir -p .agent:*)"
)
DENIED=(
  "Bash(git push origin main:*)" "Bash(git push --force:*)" "Bash(git push -f:*)" "Bash(git reset --hard:*)"
  "Bash(git config:*)" "Bash(rm -rf:*)"
)

SUMMARY="$LOG_DIR/$STAMP.summary.md"
# A run that hangs (e.g. API connections dead after the Mac slept mid-request) is killed after
# TRADEDESK_AGENTS_MAX_SECONDS so it cannot hold the lock and block the following nights.
KEEP_AWAKE=()
command -v caffeinate >/dev/null 2>&1 && KEEP_AWAKE=(caffeinate -i)
${KEEP_AWAKE[@]+"${KEEP_AWAKE[@]}"} perl -e 'alarm shift; exec @ARGV' "${TRADEDESK_AGENTS_MAX_SECONDS:-10800}" "${TRADEDESK_CLAUDE_BIN:-claude}" -p "$PROMPT" --permission-mode dontAsk \
  --allowedTools "${ALLOWED[@]}" --disallowedTools "${DENIED[@]}" | tee "$SUMMARY" || echo "claude exited with an error"

# Delivery is done here, not by the model: only agent/YYYY-MM-DD-* branches, only after lint and tests pass.
deliver() {
  [[ $DRY -eq 1 ]] && return 0
  if [[ ! -f .agent/branch || ! -f .agent/commit-msg.txt ]]; then
    echo "no delivery requested"
    return 0
  fi
  local br
  br="$(head -1 .agent/branch | tr -d '[:space:]')"
  if [[ ! "$br" =~ ^agent/[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9][a-z0-9-]*$ ]]; then
    echo "refusing branch name '$br'"
    return 1
  fi
  if git ls-remote --exit-code --heads origin "$br" >/dev/null 2>&1; then
    echo "branch $br already exists on origin"
    return 1
  fi
  if git diff --cached --quiet; then
    echo "nothing staged"
    return 1
  fi
  if ! git diff --quiet; then
    echo "unstaged changes in tracked files; refusing to deliver a commit that differs from what was tested"
    return 1
  fi
  if ! make lint || ! make test; then
    echo "lint or tests failed; not delivering"
    return 1
  fi
  # errexit is off inside a function called from `if`, so every step is checked explicitly.
  git switch -c "$br" || return 1
  git commit -q -F .agent/commit-msg.txt || return 1
  git push -q -u origin "$br" || return 1
  local repo url
  repo="$(git remote get-url origin | sed -E 's#^(git@github.com:|https://github.com/)##; s#\.git$##')"
  url="https://github.com/$repo/compare/main...$br?expand=1"
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    url="$(gh pr create --base main --head "$br" --title "$(head -1 .agent/commit-msg.txt)" --body-file .agent/commit-msg.txt)" || true
  fi
  echo "delivered $br: $url"
  printf '\n**Гілка:** `%s`\n**PR:** %s\n' "$br" "$url" >>"$SUMMARY"
}
if ! deliver; then
  printf '\n**Доставку не виконано** — причина в лозі %s.\n' "$LOG_DIR/$STAMP.log" >>"$SUMMARY"
fi

PROJECT_DIR="$(printf '%s' "$WT" | sed 's#[^A-Za-z0-9-]#-#g')"
uv run --no-project python "$REPO/scripts/agents_transcript.py" --project "$PROJECT_DIR" --out "$LOG_DIR/$STAMP.transcript.md" || true
echo "== $(date) done"
if [[ "$(uname)" == "Darwin" ]]; then
  osascript -e "display notification \"Підсумок: $SUMMARY; розмова агентів: $LOG_DIR/$STAMP.transcript.md\" with title \"tradedesk: нічний прогін агентів\"" || true
elif [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  curl -s -m 20 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=$(head -c 3800 "$SUMMARY")" >/dev/null || true
fi

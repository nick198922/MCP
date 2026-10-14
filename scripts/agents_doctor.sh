#!/bin/bash
# The doctor: a Claude session (.claude/doctor.md) that looks into a failure of the agents pipeline, or does the daily
# health check, fixes what it can in the repo and delivers the fix as an agent/YYYY-MM-DD-fix-* branch, then reports
# to Telegram. Works in its own clone. Called by scripts/agents_loop.sh.
# Usage: scripts/agents_doctor.sh "<what happened>"
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 # wait for background work instead of stopping it after 10 minutes
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WT="${TRADEDESK_AGENTS_WORKTREE:-$(dirname "$REPO")/tradedesk-agents}"
DOC="${TRADEDESK_DOCTOR_CLONE:-$(dirname "$REPO")/tradedesk-doctor}"
if [[ "$(uname)" == "Darwin" ]]; then
  LOG_DIR="${TRADEDESK_AGENTS_LOG_DIR:-$HOME/Library/Logs/tradedesk-agents}"
else
  LOG_DIR="${TRADEDESK_AGENTS_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/tradedesk-agents}"
fi
. "$REPO/scripts/agents_notify.sh"
. "$REPO/scripts/agents_deliver.sh"
REASON="${1:-daily health check}"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
REPORT="$LOG_DIR/$STAMP.doctor.md"

mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/$STAMP.doctor.log" 2>&1
echo "== $(date) doctor: $REASON"
[[ -d "$DOC/.git" ]] || git clone --quiet "$(git -C "$REPO" remote get-url origin)" "$DOC"
cd "$DOC"
ensure_git_identity
git fetch --quiet origin
if [[ -n "$(git status --porcelain)" ]]; then
  git stash push -q --include-untracked -m "doctor-leftover-$STAMP"
fi
git checkout --quiet --detach origin/main
rm -f .agent/branch .agent/commit-msg.txt
uv sync --quiet --extra dev

latest_run="$(ls -t "$LOG_DIR"/*.summary.md 2>/dev/null | head -1 || true)"
# A failure gets Opus; the routine daily check, which mostly finds nothing, Sonnet. The daily check also gets the
# agents' commands refused by the permission rules, to propose narrow allow rules for the safe ones.
MODEL="${TRADEDESK_DOCTOR_MODEL:-opus}"
DENIED_NOTE=""
if [[ "$REASON" == "daily health check" ]]; then
  MODEL="${TRADEDESK_DOCTOR_DAILY_MODEL:-sonnet}"
  PROJECT_DIR="$(printf '%s' "$WT" | sed 's#[^A-Za-z0-9-]#-#g')"
  DENIED_NOTE="

## Agents' commands refused in the last 24 h

$(uv run --no-project python "$REPO/scripts/agents_transcript.py" --project="$PROJECT_DIR" --denied 24 2>/dev/null | head -60 || echo unknown)"
fi
PROMPT="$(cat .claude/doctor.md)

## What happened

$REASON

Logs: $LOG_DIR (agent runs: <stamp>.log, <stamp>.summary.md, <stamp>.transcript.md; history.tsv; loop.log; your earlier reports: *.doctor.md). Latest run summary: ${latest_run:-none}.
The agents' clone (read it, do not change it): $WT. This server user: $(whoami), without sudo.$DENIED_NOTE"

ALLOWED=(
  Read Write Edit Glob Grep
  "Bash(uv run:*)" "Bash(uv sync:*)" "Bash(make:*)"
  "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git show:*)" "Bash(git branch:*)"
  "Bash(git fetch:*)" "Bash(git rev-parse:*)" "Bash(git ls-tree:*)" "Bash(git add:*)"
  "Bash(ls:*)" "Bash(date:*)" "Bash(head:*)" "Bash(tail:*)" "Bash(wc:*)" "Bash(grep:*)" "Bash(df:*)" "Bash(du:*)"
  "Bash(systemctl is-active:*)" "Bash(systemctl status:*)" "Bash(systemctl list-timers:*)" "Bash(mkdir -p .agent:*)"
)
DENIED=(
  "Bash(git push:*)" "Bash(git commit:*)" "Bash(git reset --hard:*)" "Bash(git config:*)" "Bash(rm -rf:*)" "Bash(sudo:*)"
  "Read(~/.claude/.credentials.json)" "Read(~/.ssh/**)" "Read(~/.config/tradedesk/**)" "Read(//etc/tradedesk-mcp.env)"
)

set +e
perl -e 'alarm shift; exec @ARGV' "${TRADEDESK_DOCTOR_MAX_SECONDS:-3600}" "${TRADEDESK_CLAUDE_BIN:-claude}" -p "$PROMPT" \
  --permission-mode dontAsk --model "$MODEL" \
  --allowedTools "${ALLOWED[@]}" --disallowedTools "${DENIED[@]}" | tee "$REPORT"
rc=${PIPESTATUS[0]}
set -e
echo "claude exit code $rc"

deliver "$REPORT" '^agent/[0-9]{4}-[0-9]{2}-[0-9]{2}-fix-[a-z0-9][a-z0-9-]*$' || echo "delivery refused: $NOTE"
[[ -n "$DELIVERED" ]] && DELIVERED_URL="$(open_pr "$DELIVERED" "$(head -1 .agent/commit-msg.txt)" "$REPORT")"
if [[ -n "$(git status --porcelain)" ]]; then
  git stash push -q --include-untracked -m "doctor-undelivered-$STAMP" || true
fi
git checkout --quiet --detach origin/main

if ((rc != 0)); then
  notify "🩺❌ **Лікар не зміг завершити огляд** (код $rc). Привід: $REASON. Лог: $LOG_DIR/$STAMP.doctor.log"
  exit 5
fi
if [[ -n "$DELIVERED" ]]; then
  notify "🩺 **Лікар** знайшов і виправив; виправлення чекає на вас.
$(head -c 3000 "$REPORT")" --button "🔀 Виправлення лікаря|$DELIVERED_URL"
elif [[ "$NOTE" != "доставку не запрошено" ]]; then
  notify "🩺 **Лікар:** виправлення не доставлено ($NOTE).
$(head -c 3000 "$REPORT")"
else
  # A routine check that found nothing to fix does not buzz.
  silent=()
  [[ "$REASON" == "daily health check" ]] && silent=(--silent)
  notify "🩺 **Лікар:**
$(head -c 3000 "$REPORT")" ${silent[@]+"${silent[@]}"}
fi

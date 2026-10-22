#!/bin/bash
# The weekly audit: a Claude session (.claude/audit.md) on a stronger model (default Fable 5.1, effort xhigh) that
# reviews the whole project (architecture, code health, docs, the agents pipeline), fixes small safe things and
# delivers its report as an agent/YYYY-MM-DD-audit-* branch and pull request, then reports to Telegram. Works in its
# own clone, so it does not disturb the agents. Run by the user timer deploy/user-units/tradedesk-audit-weekly.timer
# (Tuesdays 20:01 Kyiv, just after the weekly plan limit resets). Only this run uses that model; everything else
# keeps its own.
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CLONE="${TRADEDESK_AUDIT_CLONE:-$(dirname "$REPO")/tradedesk-audit}"
LOG_DIR="${TRADEDESK_AGENTS_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/tradedesk-agents}"
MODEL="${TRADEDESK_AUDIT_MODEL:-claude-fable-5-1}"
EFFORT="${TRADEDESK_AUDIT_EFFORT:-xhigh}"
. "$REPO/scripts/agents_notify.sh"
. "$REPO/scripts/agents_deliver.sh"
. "$REPO/scripts/agents_usage.sh"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
REPORT="$LOG_DIR/$STAMP.audit.md"

mkdir -p "$LOG_DIR"
# While the audit runs the agents loop does not start new steps (scripts/agents_loop.sh, audit_running).
echo $$ >"$LOG_DIR/audit.pid"
trap 'rm -f "$LOG_DIR/audit.pid"' EXIT
exec >>"$LOG_DIR/$STAMP.audit.log" 2>&1
echo "== $(date) weekly audit ($MODEL, effort $EFFORT)"
# The weekly limit resets at 20:00 Kyiv; if it has not (yet), wait up to an hour rather than run into the wall.
for _ in 1 2 3 4; do
  week="$(usage_now | awk '{print $2}')"
  [[ -z "$week" ]] || awk -v w="$week" 'BEGIN { exit !(w >= 90) }' || break
  echo "weekly usage ${week}%: waiting 15 min"
  sleep 900
done
if [[ -n "${week:-}" ]] && awk -v w="$week" 'BEGIN { exit !(w >= 90) }'; then
  notify "🔎❌ **Тижневий аудит пропущено:** тижневий ліміт ${week}%. Запустити вручну: \`bash scripts/agents_audit.sh\`."
  exit 4
fi
[[ -d "$CLONE/.git" ]] || git clone --quiet "$(git -C "$REPO" remote get-url origin)" "$CLONE"
cd "$CLONE"
ensure_git_identity
git fetch --quiet origin
if [[ -n "$(git status --porcelain)" ]]; then
  git stash push -q --include-untracked -m "audit-leftover-$STAMP"
fi
git checkout --quiet --detach origin/main
rm -f .agent/branch .agent/commit-msg.txt
uv sync --quiet --extra dev

PROMPT="$(cat .claude/audit.md)

## Context

Today: $(date +%F). Agents' logs (read them, do not change them): $LOG_DIR. The agents' clone: $(dirname "$REPO")/tradedesk-agents. This server user: $(whoami), without sudo."

ALLOWED=(
  Read Write Edit Glob Grep
  "Bash(uv run:*)" "Bash(uv sync:*)" "Bash(make:*)"
  "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git show:*)" "Bash(git branch:*)"
  "Bash(git fetch:*)" "Bash(git rev-parse:*)" "Bash(git ls-tree:*)" "Bash(git ls-files:*)" "Bash(git grep:*)" "Bash(git add:*)"
  "Bash(ls:*)" "Bash(date:*)" "Bash(head:*)" "Bash(tail:*)" "Bash(wc:*)" "Bash(grep:*)" "Bash(df:*)" "Bash(du:*)"
  "Bash(systemctl is-active:*)" "Bash(systemctl --user list-timers:*)" "Bash(mkdir -p .agent:*)"
)
DENIED=(
  "Bash(git push:*)" "Bash(git commit:*)" "Bash(git reset --hard:*)" "Bash(git config:*)" "Bash(rm -rf:*)" "Bash(sudo:*)"
  "Read(~/.claude/.credentials.json)" "Read(~/.ssh/**)" "Read(~/.config/tradedesk/**)" "Read(//etc/tradedesk-mcp.env)"
)

before="$(usage_now)"
set +e
perl -e 'alarm shift; exec @ARGV' "${TRADEDESK_AUDIT_MAX_SECONDS:-10800}" "${TRADEDESK_CLAUDE_BIN:-claude}" -p "$PROMPT" \
  --permission-mode dontAsk --model "$MODEL" --effort "$EFFORT" --add-dir "$LOG_DIR" \
  --allowedTools "${ALLOWED[@]}" --disallowedTools "${DENIED[@]}" | tee "$REPORT"
rc=${PIPESTATUS[0]}
set -e
echo "claude exit code $rc"
after="$(usage_now)"
spent="$(usage_change "$before" "$after")"
# The audit's share of this week's limit, for the agents' pace (scripts/agents_loop.sh, pace_wait).
week="$(usage_json | jq -r '.seven_day.resets_at // empty' 2>/dev/null || true)"
cost="$(awk -v a="$before" -v b="$after" 'BEGIN { split(a, x, " "); split(b, y, " "); if (y[2] > x[2]) printf "%.1f", y[2] - x[2] }')"
[[ -n "$week" && -n "$cost" ]] && printf '%s %s\n' "$week" "$cost" >"$LOG_DIR/audit.cost"

deliver "$REPORT" '^agent/[0-9]{4}-[0-9]{2}-[0-9]{2}-audit-[a-z0-9][a-z0-9-]*$' || echo "delivery refused: $NOTE"
[[ -n "$DELIVERED" ]] && DELIVERED_URL="$(open_pr "$DELIVERED" "$(head -1 .agent/commit-msg.txt)" "$REPORT" --ready)"
if [[ -n "$(git status --porcelain)" ]]; then
  git stash push -q --include-untracked -m "audit-undelivered-$STAMP" || true
fi
git checkout --quiet --detach origin/main

if ((rc != 0)); then
  notify "🔎❌ **Тижневий аудит не завершився** (код $rc). Лог: $LOG_DIR/$STAMP.audit.log"
  exit 5
fi
buttons=()
[[ -n "$DELIVERED" ]] && buttons=(--button "🔀 Звіт аудиту|$DELIVERED_URL")
notify "🔎 **Тижневий аудит** ($MODEL, $EFFORT${spent:+; ліміт: $spent})
$(head -c 3000 "$REPORT")${NOTE:+
Доставка: $NOTE}" ${buttons[@]+"${buttons[@]}"}

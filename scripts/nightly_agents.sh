#!/bin/bash
# One pipeline step by the project agents, in a separate clone of the repo; the result is an agent/* branch.
# Usage: scripts/nightly_agents.sh [--dry-run]
# Nothing is lost silently: a step that ends without delivery leaves its work in the agents' clone, the next run
# resumes it (up to TRADEDESK_AGENTS_MAX_RESUMES times), and after that the work is pushed as an agent/*-wip branch.
# Every outcome goes to Telegram (scripts/agents_notify.sh) and to $LOG_DIR/history.tsv.
# Exit codes for scripts/agents_loop.sh: 0 delivered (or dry run / work rescued), 3 nothing to do,
# 4 unfinished (the next run resumes it), 5 claude failed.
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Subagents run in the background. By default `claude -p` stops background work 10 minutes after the coordinator's
# last reply, which killed a quant in the middle of a long fetch; wait for it instead (the run's alarm still applies).
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WT="${TRADEDESK_AGENTS_WORKTREE:-$(dirname "$REPO")/tradedesk-agents}"
if [[ "$(uname)" == "Darwin" ]]; then
  LOG_DIR="${TRADEDESK_AGENTS_LOG_DIR:-$HOME/Library/Logs/tradedesk-agents}"
else
  LOG_DIR="${TRADEDESK_AGENTS_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/tradedesk-agents}"
fi
. "$REPO/scripts/agents_notify.sh"
. "$REPO/scripts/agents_deliver.sh"
MAX_RESUMES="${TRADEDESK_AGENTS_MAX_RESUMES:-2}"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/$STAMP.log" 2>&1
# The lock holds the owner's pid, so a lock left by a killed run or a reboot does not block every later run.
LOCK="$LOG_DIR/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  pid="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "another run ($pid) holds $LOCK; exiting"
    exit 0
  fi
  echo "removing a stale lock (pid ${pid:-unknown} is gone)"
  rm -f "$LOCK/pid"
  rmdir "$LOCK"
  mkdir "$LOCK"
fi
echo $$ >"$LOCK/pid"
trap 'rm -f "$LOCK/pid"; rmdir "$LOCK"' EXIT

STATE="$LOG_DIR/resume.state" # "<resume attempts> <stamp of the run that started the unfinished step>"
SUMMARY="$LOG_DIR/$STAMP.summary.md"
: >"$SUMMARY"
dirty() { [[ -n "$(git -C "$WT" status --porcelain 2>/dev/null)" ]]; }

# Report one outcome: history line, Telegram message, exit code.
# Arguments after the text go to notify (--silent, --button "Label|url").
finish() {
  local code="$1" status="$2" text="$3" last
  shift 3
  last="$(tail -1 "$LOG_DIR/history.tsv" 2>/dev/null | cut -f2 || true)"
  printf '%s\t%s\t%s\n' "$STAMP" "$status" "$(head -1 <<<"$text")" >>"$LOG_DIR/history.tsv"
  echo "== $(date) done: $status"
  # A repeated "nothing to do" or failure is reported once; the loop tells the owner when failures pile up.
  if [[ "$status" != "$last" || ("$status" != "idle" && "$status" != "error") ]]; then
    notify "$text" "$@"
  fi
  exit "$code"
}

echo "== $(date) start (dry=$DRY)"
git -C "$REPO" fetch --quiet origin
# The agents work in a separate clone, not a git worktree: Claude Code applies the main checkout's project
# settings (the owner's broad permissions in .claude/settings.local.json) to its worktrees as well.
# Unfinished work in an old worktree is carried over to the clone, on the commit it was based on.
if [[ -e "$WT/.git" && ! -d "$WT/.git" ]]; then
  carry=""
  if dirty; then
    git -C "$WT" add -A
    git -C "$WT" -c user.name=agents -c user.email=agents@localhost commit -q -m "agents leftover $STAMP"
    carry="refs/agents/leftover-$STAMP"
    git -C "$REPO" update-ref "$carry" "$(git -C "$WT" rev-parse HEAD)"
  fi
  git -C "$REPO" worktree remove --force "$WT"
  git clone --quiet "$(git -C "$REPO" remote get-url origin)" "$WT"
  if [[ -n "$carry" ]]; then
    git -C "$WT" fetch --quiet "$REPO" "$carry"
    git -C "$WT" checkout --quiet --detach FETCH_HEAD
    git -C "$WT" reset --quiet --soft HEAD~1
    git -C "$WT" reset --quiet
  fi
fi
if [[ ! -d "$WT/.git" ]]; then
  git clone --quiet "$(git -C "$REPO" remote get-url origin)" "$WT"
fi
cd "$WT"
ensure_git_identity
git fetch --quiet origin
rm -f .agent/branch .agent/commit-msg.txt

# Push work that could not be finished after MAX_RESUMES attempts, so the owner sees it and nothing is lost.
RESCUE_FAILED=""
rescue_wip() {
  local first="$1" attempts="$2" br checks="lint and tests pass" url
  if [[ -z "$(git status --porcelain -- docs src tests scripts)" ]]; then
    git stash push -q --include-untracked -m "agents-leftover-$STAMP" || true
    git checkout --quiet --detach origin/main
    rm -f "$STATE"
    return 0
  fi
  br="agent/$(date +%F)-wip-$(date +%H%M%S)"
  make lint >"$LOG_DIR/$STAMP.rescue-lint.log" 2>&1 || checks="LINT FAILS"
  make test >"$LOG_DIR/$STAMP.rescue-test.log" 2>&1 || checks="$checks; TESTS FAIL"
  git switch -q -c "$br" || { RESCUE_FAILED="git switch"; return 1; }
  git add -A -- docs src tests scripts || { RESCUE_FAILED="git add"; return 1; }
  while IFS= read -r f; do
    if [[ -f "$f" && $(wc -c <"$f") -gt 2000000 ]]; then
      git reset -q -- "$f"
      echo "left out (over 2 MB): $f"
    fi
  done < <(git diff --cached --name-only)
  git diff --cached --quiet && { RESCUE_FAILED="nothing staged"; return 1; }
  git commit -q -F - <<EOF || { RESCUE_FAILED="git commit"; return 1; }
Unfinished agent step, pushed as is ($checks)

The step started at $first and was resumed $attempts times without
being delivered, so the launcher pushed its work instead of losing it.
Not a final result: see the run summaries and the agents' transcripts.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
  git push -q -u origin "$br" || { RESCUE_FAILED="git push"; return 1; }
  url="https://github.com/$(repo_slug)/compare/main...$br?expand=1"
  # Anything left outside the project paths is set aside so the next run starts clean.
  if dirty; then git stash push -q --include-untracked -m "agents-rescue-leftover-$STAMP" || true; fi
  git checkout --quiet --detach origin/main
  rm -f "$STATE"
  finish 0 "wip" "⚠️ **Агенти: крок не завершено і після $attempts повторних спроб.** Роботу збережено гілкою \`$br\` ($checks); лікар уже шукає причину." --button "🔀 Подивитися гілку|$url"
}

rescue_failed() {
  finish 5 "error" "❌ **Агенти: не вдалося зберегти незавершену роботу гілкою** (впало: ${RESCUE_FAILED:-?}). Лог: $LOG_DIR/$STAMP.log
$(grep -v '^== ' "$LOG_DIR/$STAMP.log" | tail -5)"
}

MODE=fresh
attempts=0
first="$STAMP"
if dirty; then
  if [[ $DRY -eq 1 ]]; then
    echo "unfinished work in $WT; dry run skipped"
    exit 0
  fi
  read -r attempts first <"$STATE" 2>/dev/null || { attempts=0; first="$STAMP"; }
  if ((attempts >= MAX_RESUMES)); then
    rescue_wip "$first" "$attempts" || rescue_failed
  fi
  MODE=resume
  attempts=$((attempts + 1))
  echo "$attempts $first" >"$STATE"
else
  rm -f "$STATE" "$LOG_DIR/refusal.txt"
  git checkout --quiet --detach origin/main
fi
uv sync --quiet --extra dev
echo "agents clone $WT at $(git rev-parse --short HEAD), mode $MODE"

PROMPT="$(cat .claude/nightly.md)"
if [[ $MODE == resume ]]; then
  # The agents may read only inside the clone, so the previous run's transcript is copied into .agent/ (gitignored).
  prev="$(ls -t "$LOG_DIR"/*.transcript.md 2>/dev/null | head -1 || true)"
  hint=""
  if [[ -n "$prev" ]]; then
    mkdir -p .agent
    cp "$prev" .agent/previous-transcript.md
    hint=", and the transcript of the previous run: .agent/previous-transcript.md (read it with the Read tool)"
  fi
  refused=""
  if [[ -s "$LOG_DIR/refusal.txt" ]]; then
    refused="

The launcher refused the previous delivery. Fix exactly this before you request delivery again:
$(cat "$LOG_DIR/refusal.txt")"
  fi
  PROMPT="$PROMPT

RESUME (attempt $attempts of $MAX_RESUMES): the previous run ended before delivery. The uncommitted changes in this tree (git status, git diff, git diff --cached) are its work, and HEAD is the commit it started from; ignore the 'clean tree at origin/main' and work-in-progress-limit parts of the preflight. Do not start a new step and do not discard that work. Find where it stopped (the card in docs/ideas, docs/reviews, docs/verification$hint). Finish the step if little is left; otherwise go straight to steps 3-4 with what exists and say in the card and the summary what is unfinished. Stage every file of the step, including the ones the previous run changed: the launcher refuses a delivery with unstaged changes.$refused"
fi
if [[ $DRY -eq 1 ]]; then
  PROMPT="$PROMPT

DRY RUN: do steps 0-2 only (read and decide). Make no file changes, no commits, no pushes, and do not start subagents. Print the plan you would execute."
fi

ALLOWED=(
  Read Write Edit Glob Grep WebSearch WebFetch Agent
  "Bash(uv run:*)" "Bash(uv sync:*)" "Bash(make:*)"
  "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git show:*)" "Bash(git branch:*)"
  "Bash(git fetch:*)" "Bash(git rev-parse:*)" "Bash(git ls-tree:*)" "Bash(git ls-files:*)" "Bash(git add:*)"
  "Bash(ls:*)" "Bash(date:*)" "Bash(which:*)" "Bash(head:*)" "Bash(tail:*)" "Bash(wc:*)" "Bash(mkdir -p .agent:*)"
  "Bash(cat:*)" "Bash(sed:*)" "Bash(cut:*)" "Bash(grep:*)" "Bash(sort:*)" "Bash(uniq:*)" "Bash(find:*)"
  "Bash(scripts/agents_wait.sh:*)" "Bash(bash scripts/agents_wait.sh:*)"
)
DENIED=(
  "Bash(git push origin main:*)" "Bash(git push --force:*)" "Bash(git push -f:*)" "Bash(git reset --hard:*)"
  "Bash(git config:*)" "Bash(rm -rf:*)" "Bash(sudo:*)"
  "Read(~/.claude/.credentials.json)" "Read(~/.ssh/**)" "Read(~/.config/tradedesk/**)" "Read(//etc/tradedesk-mcp.env)"
)

# The coordinator only routes and stages, so it runs on Sonnet; each subagent keeps the model from its
# .claude/agents/*.md (quant and critic: Opus).
# A run that hangs is killed after TRADEDESK_AGENTS_MAX_SECONDS; its work stays for the next run to resume.
# Research data that outlives the clone (scripts' --cache-dir) is kept in CACHE_DIR, which the agents may also read.
KEEP_AWAKE=()
command -v caffeinate >/dev/null 2>&1 && KEEP_AWAKE=(caffeinate -i)
CACHE_DIR="${TRADEDESK_AGENTS_CACHE_DIR:-$HOME/.cache/tradedesk-agents}"
mkdir -p "$CACHE_DIR"
set +e
${KEEP_AWAKE[@]+"${KEEP_AWAKE[@]}"} perl -e 'alarm shift; exec @ARGV' "${TRADEDESK_AGENTS_MAX_SECONDS:-10800}" "${TRADEDESK_CLAUDE_BIN:-claude}" -p "$PROMPT" --permission-mode dontAsk \
  --model "${TRADEDESK_AGENTS_COORDINATOR_MODEL:-sonnet}" --add-dir "$CACHE_DIR" \
  --allowedTools "${ALLOWED[@]}" --disallowedTools "${DENIED[@]}" | tee -a "$SUMMARY"
rc=${PIPESTATUS[0]}
set -e
echo "claude exit code $rc"

if [[ $DRY -eq 0 ]]; then
  deliver "$SUMMARY" '^agent/[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9][a-z0-9-]*$' || echo "delivery refused: $NOTE"
fi

PROJECT_DIR="$(printf '%s' "$WT" | sed 's#[^A-Za-z0-9-]#-#g')"
uv run --no-project python "$REPO/scripts/agents_transcript.py" --project="$PROJECT_DIR" --out "$LOG_DIR/$STAMP.transcript.md" || true

BODY="$(head -c 3000 "$SUMMARY")"
if [[ $DRY -eq 1 ]]; then
  finish 0 "dry" "🧪 **Агенти: пробний прогін**
$BODY" --silent
elif [[ -n "$DELIVERED" ]]; then
  rm -f "$STATE" "$LOG_DIR/refusal.txt"
  finish 0 "delivered" "✅ **Агенти здали крок:** \`$DELIVERED\`
$BODY" --button "🔀 Відкрити PR|$DELIVERED_URL"
elif dirty; then
  why="${NOTE:-координатор завершився, не здавши крок}"
  if ((rc != 0)); then why="$why; claude завершився з кодом $rc"; fi
  if ((attempts >= MAX_RESUMES)); then
    rescue_wip "$first" "$attempts" || rescue_failed
  fi
  echo "$attempts $first" >"$STATE"
  printf '%s\n%s\n' "$why" "$DETAIL" >"$LOG_DIR/refusal.txt"
  finish 4 "unfinished" "⏳ **Агенти: крок не здано з першого разу** ($why). Нічого робити не треба: за кілька хвилин наступний прогін продовжить роботу (спроба $((attempts + 1)) з $MAX_RESUMES), а якщо не вийде — збереже її гілкою wip." --silent
elif ((rc != 0)); then
  finish 5 "error" "❌ **Агенти: claude завершився з помилкою** (код $rc), змін немає; лікар шукає причину. Кінець виводу:
$(tail -c 800 "$SUMMARY")"
else
  finish 3 "idle" "💤 **Агенти: роботи немає.** ${BODY:0:600}" --silent
fi

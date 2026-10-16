#!/bin/bash
# One pipeline step by the project agents, in a separate clone of the repo; the result is an agent/* branch.
# Usage: scripts/nightly_agents.sh [--dry-run]
# Work the agents have not finished stays theirs: it is pushed to a work branch (on a delivery the coordinator marks
# "continue", or saved by this script when a run ends without one), and the next run checks that branch out and
# carries on. The owner gets a pull request only when the coordinator hands the step over ("owner": the critic
# approved, the card ended, or a question waits) or after TRADEDESK_AGENTS_MAX_RUNS runs on the same work.
# Every outcome goes to Telegram (scripts/agents_notify.sh) and to $LOG_DIR/history.tsv.
# Exit codes for scripts/agents_loop.sh: 0 handed to the owner (or dry run), 3 nothing to do,
# 4 the work continues in the next run, 5 claude or saving failed.
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
. "$REPO/scripts/agents_usage.sh"
MAX_RUNS="${TRADEDESK_AGENTS_MAX_RUNS:-5}"
BRANCH_RE='^agent/[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9][a-z0-9-]*$'
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

WORK="$LOG_DIR/work.state" # "<work branch> <runs on it> <stamp of its first run> [<killed runs in a row>]"
ACTIVE="$LOG_DIR/run.active" # exists while a run is under way; left behind, it marks a run killed before it reported
PARKED="$LOG_DIR/parked.state" # "<branch> <not-before epoch> <runs> <first>": work set aside until a time (".agent/next park")
SUMMARY="$LOG_DIR/$STAMP.summary.md"
: >"$SUMMARY"
dirty() { [[ -n "$(git -C "$WT" status --porcelain 2>/dev/null)" ]]; }

# Report one outcome: history line (with what the step cost), Telegram message, exit code.
# Arguments after the text go to notify (--silent, --button "Label|url").
COST=""
finish() {
  local code="$1" status="$2" text="$3" last rest
  shift 3
  rm -f "$ACTIVE"
  last="$(tail -1 "$LOG_DIR/history.tsv" 2>/dev/null | cut -f2 || true)"
  printf '%s\t%s\t%s\t%s\n' "$STAMP" "$status" "$(head -1 <<<"$text")" "$COST" >>"$LOG_DIR/history.tsv"
  if [[ -n "$COST" ]]; then
    rest="$(tail -n +2 <<<"$text")"
    text="$(head -1 <<<"$text")
$COST${rest:+
$rest}"
  fi
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
git fetch --quiet --prune origin
rm -f .agent/branch .agent/commit-msg.txt .agent/next "$LOG_DIR/resume.state"

# The work in progress, unless the owner has merged or deleted its branch meanwhile.
WORK_BR="" RUNS=0 FIRST="$STAMP" KILLS=0
read -r WORK_BR RUNS FIRST KILLS 2>/dev/null <"$WORK" || true
RUNS="${RUNS:-0}" FIRST="${FIRST:-$STAMP}" KILLS="${KILLS:-0}"
if [[ -n "$WORK_BR" ]]; then
  if ! git rev-parse -q --verify "refs/remotes/origin/$WORK_BR" >/dev/null || git merge-base --is-ancestor "origin/$WORK_BR" origin/main; then
    echo "work branch $WORK_BR was merged or deleted; starting afresh"
    if dirty; then git stash push -q --include-untracked -m "agents-leftover-$STAMP" || true; fi
    WORK_BR="" RUNS=0 FIRST="$STAMP" KILLS=0
    rm -f "$WORK" "$LOG_DIR/refusal.txt"
  fi
fi

# Parked work (it waits for a time, e.g. the next pass of a forward observation) resumes once that time has come and no
# other work is in progress; until then the runs take other steps. A merged or deleted parked branch is forgotten.
PARKED_NOTE=""
if [[ -s "$PARKED" ]]; then
  read -r P_BR P_AT P_RUNS P_FIRST 2>/dev/null <"$PARKED" || true
  if [[ -z "${P_BR:-}" ]] || ! git rev-parse -q --verify "refs/remotes/origin/$P_BR" >/dev/null || git merge-base --is-ancestor "origin/$P_BR" origin/main; then
    echo "parked branch ${P_BR:-?} was merged or deleted; forgetting it"
    rm -f "$PARKED"
  elif [[ -z "$WORK_BR" ]] && (($(date +%s) >= ${P_AT:-0})); then
    echo "resuming parked work $P_BR"
    WORK_BR="$P_BR" RUNS="${P_RUNS:-1}" FIRST="${P_FIRST:-$STAMP}" KILLS=0
    rm -f "$PARKED"
  else
    PARKED_NOTE="$P_BR"
  fi
fi

MODE=fresh
if [[ $DRY -eq 1 ]]; then
  if [[ -n "$WORK_BR" ]] || dirty; then
    echo "work in progress; dry run skipped"
    exit 0
  fi
  git checkout --quiet --detach origin/main
elif [[ -n "$WORK_BR" ]]; then
  MODE=continue
  # Keep the local tree only when it is a killed run's work on top of the saved work branch. Anything else (a clone
  # left at main with a stray helper file, as on 2026-10-15) is set aside, and the run starts from the saved work.
  if dirty && git merge-base --is-ancestor "origin/$WORK_BR" HEAD 2>/dev/null; then
    echo "continuing a killed run's changes on top of $WORK_BR"
  else
    if dirty; then git stash push -q --include-untracked -m "agents-leftover-$STAMP" || true; fi
    git checkout --quiet -B "$WORK_BR" "origin/$WORK_BR"
    # Keep the work current with main so its PR merges cleanly; a conflict is left for the owner's review.
    git merge --quiet --no-edit origin/main >/dev/null 2>&1 || git merge --abort 2>/dev/null || true
  fi
elif dirty; then
  MODE=continue # a killed run's work that was never saved
else
  rm -f "$LOG_DIR/refusal.txt"
  git checkout --quiet --detach origin/main
fi
# A run killed before it reported (the service restarted after an OOM kill, a reboot) did not move the step forward:
# its number is reused. On 2026-10-16 five such runs used up MAX_RUNS. After MAX_RUNS kills in a row the runs count
# again, so work that gets killed every time still reaches the owner.
if [[ $MODE == continue && -n "$WORK_BR" && -e "$ACTIVE" ]] && ((RUNS > 0 && KILLS < MAX_RUNS)); then
  KILLS=$((KILLS + 1))
  echo "run $RUNS was killed before it reported; it is repeated ($KILLS in a row)"
elif [[ $MODE == continue ]]; then
  RUNS=$((RUNS + 1)) KILLS=0 # the previous run reported: the kills are no longer in a row
else
  RUNS=1 FIRST="$STAMP" KILLS=0
fi
[[ -n "$WORK_BR" ]] && echo "$WORK_BR $RUNS $FIRST $KILLS" >"$WORK"
echo "$STAMP" >"$ACTIVE"
uv sync --quiet --extra dev
echo "agents clone $WT at $(git rev-parse --short HEAD), mode $MODE, run $RUNS${WORK_BR:+ on $WORK_BR}"

# Save the work of a run that did not hand anything over: commit the project paths on the work branch (a new one if
# there is none yet) and push it, so nothing is lost and the next run continues from GitHub. Returns 2 when there is
# nothing to save.
SAVE_FAILED="" SAVE_CHECKS=""
save_work() {
  local br="$WORK_BR" want f
  if [[ -z "$br" ]]; then
    if [[ -z "$(git status --porcelain -- docs src tests scripts)" ]]; then
      if dirty; then git stash push -q --include-untracked -m "agents-leftover-$STAMP" || true; fi
      git checkout --quiet --detach origin/main
      return 2
    fi
    want="$(head -1 .agent/branch 2>/dev/null | tr -d '[:space:]')"
    if [[ "$want" =~ $BRANCH_RE ]] && ! git ls-remote --exit-code --heads origin "$want" >/dev/null 2>&1; then
      br="$want"
    else
      br="agent/$(date +%F)-wip-$(date +%H%M%S)"
    fi
  fi
  if [[ -n "$WORK_BR" ]] && git rev-parse -q --verify "refs/remotes/origin/$br" >/dev/null &&
    ! git merge-base --is-ancestor "origin/$br" HEAD 2>/dev/null; then
    SAVE_FAILED="the clone is not on $br (it does not contain the saved work), nothing pushed"
    return 1
  fi
  SAVE_CHECKS="lint and tests pass"
  make lint >"$LOG_DIR/$STAMP.save-lint.log" 2>&1 || SAVE_CHECKS="LINT FAILS"
  make test >"$LOG_DIR/$STAMP.save-test.log" 2>&1 || SAVE_CHECKS="$SAVE_CHECKS; TESTS FAIL"
  git switch -q -C "$br" || { SAVE_FAILED="git switch"; return 1; }
  # scripts/_* are the agents' throwaway helpers (.claude/nightly.md), never project files.
  git add -A -- docs src tests scripts ':!scripts/_*' || { SAVE_FAILED="git add"; return 1; }
  while IFS= read -r f; do
    if [[ -f "$f" && $(wc -c <"$f") -gt 2000000 ]]; then
      git reset -q -- "$f"
      echo "left out (over 2 MB): $f"
    fi
  done < <(git diff --cached --name-only)
  if ! git diff --cached --quiet; then
    git commit -q -F - <<EOF || { SAVE_FAILED="git commit"; return 1; }
Agents' work in progress, run $RUNS ($SAVE_CHECKS)

Saved by the launcher at $STAMP: the run ended without handing the step
over, and the next run continues from here. Not a final result.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
  fi
  git push -q -u origin "$br" || { SAVE_FAILED="git push"; return 1; }
  # Anything left outside the project paths is set aside so the next run starts clean.
  if dirty; then git stash push -q --include-untracked -m "agents-save-leftover-$STAMP" || true; fi
  WORK_BR="$br"
}

# Hand the work to the owner: a pull request (or a compare link without a GitHub token) and a Telegram message.
# A finished step ("delivered") is marked ready: GitHub merges it by itself once CI passes, unless it changes the
# control plane (scripts/automerge.py); a stuck one always waits for the owner.
handover() {
  local br="$1" status="$2" head="$3" title url ready="" verdict how
  title="$(head -1 .agent/commit-msg.txt 2>/dev/null || true)"
  verdict="$(git diff --name-only "origin/main...$br" | python3 "$REPO/scripts/automerge.py" --classify 2>/dev/null || echo "owner: ?")"
  if [[ "$status" == delivered && "$verdict" == auto ]]; then
    ready="--ready"
    how="🤖 Зіллється сам, щойно на GitHub пройдуть тести."
  else
    how="👀 Чекає на ваше злиття (${verdict#owner: })."
  fi
  url="$(open_pr "$br" "${title:-Agents: $br}" "$SUMMARY" $ready)"
  rm -f "$WORK" "$LOG_DIR/refusal.txt"
  finish 0 "$status" "$head
$how
$(head -c 3000 "$SUMMARY")" --button "🔀 Відкрити PR|$url"
}

PROMPT="$(cat .claude/nightly.md)"
if [[ $MODE == continue ]]; then
  # The agents may read only inside the clone, so the previous run's transcript is copied into .agent/ (gitignored).
  prev="$(ls -t "$LOG_DIR"/*.transcript.md 2>/dev/null | head -1 || true)"
  hint=""
  if [[ -n "$prev" ]]; then
    mkdir -p .agent
    cp "$prev" .agent/previous-transcript.md
    hint=", and the transcript of the previous run: .agent/previous-transcript.md (read it with the Read tool)"
  fi
  on_branch=""
  [[ -n "$WORK_BR" ]] && on_branch=" Its commits are on this branch, \`$WORK_BR\`, beyond origin/main (git log --oneline origin/main..HEAD, git diff --stat origin/main...HEAD); this branch is not waiting for the owner, so its cards are not skipped."
  refused=""
  if [[ -s "$LOG_DIR/refusal.txt" ]]; then
    refused="

The launcher refused the previous delivery. Fix exactly this before you request delivery again:
$(cat "$LOG_DIR/refusal.txt")"
  fi
  PROMPT="$PROMPT

CONTINUE (run $RUNS of at most $MAX_RUNS on this work): an earlier run started this step and did not hand it to the owner.$on_branch Uncommitted changes (git status) are its work too. Do not start another step and do not discard that work. Find where it stopped (the card in docs/ideas, docs/reviews, docs/verification, docs/agents/runs$hint) and carry on from there; ignore the 'clean tree at origin/main' and work-in-progress-limit parts of the preflight (this work adds no new branch). Stage every file of the step, including the ones earlier runs changed. Any valid name in .agent/branch will do: the launcher keeps this work on its branch.$refused"
fi
if [[ -n "$PARKED_NOTE" ]]; then
  PROMPT="$PROMPT

PARKED: \`$PARKED_NOTE\` is work set aside until a set time (a later run resumes it). Do not take its card or item, and do not count its branch in the work-in-progress limit."
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
  "Bash(scripts/agents_wait.sh:*)" "Bash(bash scripts/agents_wait.sh:*)" "Bash($WT/scripts/agents_wait.sh:*)"
  # Inert or read-only commands the agents reach for in compound commands (each part of one is checked on its own);
  # python3 runs code like the allowed `uv run python`. git -C, git checkout, kill and file redirections stay off.
  "Bash(cd:*)" "Bash(mkdir:*)" "Bash(printf:*)" "Bash(echo:*)" "Bash(ps:*)" "Bash(free:*)" "Bash(stat:*)"
  "Bash(awk:*)" "Bash(tr:*)" "Bash(diff:*)" "Bash(jq:*)" "Bash(python3:*)"
  "Bash(scripts/agents_rm.py:*)" "Bash(python3 scripts/agents_rm.py:*)" "Bash(uv run python scripts/agents_rm.py:*)"
)
DENIED=(
  "Bash(git push origin main:*)" "Bash(git push --force:*)" "Bash(git push -f:*)" "Bash(git reset --hard:*)"
  "Bash(git config:*)" "Bash(rm -rf:*)" "Bash(sudo:*)"
  "Read(~/.claude/.credentials.json)" "Read(~/.ssh/**)" "Read(~/.config/tradedesk/**)" "Read(//etc/tradedesk-mcp.env)"
)

# The coordinator only routes and stages, so it runs on Sonnet; each subagent keeps the model from its
# .claude/agents/*.md (quant and critic: Opus).
# A run that hangs is killed after TRADEDESK_AGENTS_MAX_SECONDS (5 h: a research step with a long fetch, the
# quant's work and the critic's review took almost 3); its work is saved for the next run.
# Research data that outlives the clone (scripts' --cache-dir) is kept in CACHE_DIR, which the agents may also read.
KEEP_AWAKE=()
command -v caffeinate >/dev/null 2>&1 && KEEP_AWAKE=(caffeinate -i)
CACHE_DIR="${TRADEDESK_AGENTS_CACHE_DIR:-$HOME/.cache/tradedesk-agents}"
mkdir -p "$CACHE_DIR"
T0="$(date +%s)"
U0="$(usage_now)"
set +e
${KEEP_AWAKE[@]+"${KEEP_AWAKE[@]}"} perl -e 'alarm shift; exec @ARGV' "${TRADEDESK_AGENTS_MAX_SECONDS:-18000}" "${TRADEDESK_CLAUDE_BIN:-claude}" -p "$PROMPT" --permission-mode dontAsk \
  --model "${TRADEDESK_AGENTS_COORDINATOR_MODEL:-sonnet}" --add-dir "$CACHE_DIR" \
  --allowedTools "${ALLOWED[@]}" --disallowedTools "${DENIED[@]}" | tee -a "$SUMMARY"
rc=${PIPESTATUS[0]}
set -e
echo "claude exit code $rc"
secs=$(($(date +%s) - T0))
COST="⏱ $((secs / 3600)) год $((secs % 3600 / 60)) хв"
change="$(usage_change "$U0" "$(usage_now)")"
[[ -n "$change" ]] && COST="$COST · ліміт Claude: $change"

NEXT="$(head -1 .agent/next 2>/dev/null | tr -d '[:space:]' || true)"
NOT_BEFORE=""
if [[ "$NEXT" == park ]]; then
  # An ISO time in UTC, at most 24 h ahead; anything else turns the park into an ordinary continue.
  NOT_BEFORE="$(python3 -c 'import sys, time, datetime as d
t = d.datetime.fromisoformat(sys.argv[1].strip().replace("Z", "+00:00"))
t = t if t.tzinfo else t.replace(tzinfo=d.timezone.utc)
e = int(t.timestamp())
print(e if time.time() < e <= time.time() + 86400 else "")' "$(head -1 .agent/not_before 2>/dev/null)" 2>/dev/null || true)"
  [[ -z "$NOT_BEFORE" ]] && NEXT=continue
fi
if [[ $DRY -eq 0 ]]; then
  deliver "$SUMMARY" "$BRANCH_RE" "$WORK_BR" || echo "delivery refused: $NOTE"
fi

PROJECT_DIR="$(printf '%s' "$WT" | sed 's#[^A-Za-z0-9-]#-#g')"
uv run --no-project python "$REPO/scripts/agents_transcript.py" --project="$PROJECT_DIR" --out "$LOG_DIR/$STAMP.transcript.md" || true

if [[ $DRY -eq 1 ]]; then
  finish 0 "dry" "🧪 **Агенти: пробний прогін**
$(head -c 3000 "$SUMMARY")" --silent
elif [[ -n "$DELIVERED" ]]; then
  rm -f "$LOG_DIR/refusal.txt"
  if [[ "$NEXT" == park ]] && ((RUNS < MAX_RUNS)); then
    echo "$DELIVERED $NOT_BEFORE $RUNS $FIRST" >"$PARKED"
    rm -f "$WORK"
    finish 4 "parked" "⏸ **Агенти: крок відкладено** (\`$DELIVERED\`, прогін $RUNS з $MAX_RUNS) до $(TZ=Europe/Kyiv date -d "@$NOT_BEFORE" '+%d.%m %H:%M' 2>/dev/null || echo "$NOT_BEFORE") за Києвом; тим часом агенти беруть інші кроки, потім продовжать його самі.
$(head -c 1500 "$SUMMARY")" --silent
  fi
  if [[ "$NEXT" == continue ]] && ((RUNS < MAX_RUNS)); then
    echo "$DELIVERED $RUNS $FIRST" >"$WORK"
    finish 4 "continues" "🔄 **Агенти: проміжний крок збережено** (\`$DELIVERED\`, прогін $RUNS з $MAX_RUNS); наступний прогін продовжить сам, вам нічого робити не треба.
$(head -c 1500 "$SUMMARY")" --silent
  fi
  head="✅ **Агенти здали крок:** \`$DELIVERED\`"
  [[ "$NEXT" == continue ]] && head="⚠️ **Агенти: крок ще не завершено, але це вже $RUNS-й прогін;** віддаю вам як є: \`$DELIVERED\`"
  handover "$DELIVERED" "delivered" "$head"
elif dirty || [[ -n "$WORK_BR" ]]; then
  why="${NOTE:-координатор завершився, не здавши крок}"
  if ((rc != 0)); then why="$why; claude завершився з кодом $rc"; fi
  set +e
  save_work
  saved=$?
  set -e
  if ((saved == 1)); then
    # A run whose work could not be saved did not move the step forward: it does not count toward MAX_RUNS.
    if [[ $MODE == continue && -n "$WORK_BR" ]] && ((RUNS > 1)); then echo "$WORK_BR $((RUNS - 1)) $FIRST" >"$WORK"; fi
    finish 5 "error" "❌ **Агенти: не вдалося зберегти роботу гілкою** (впало: ${SAVE_FAILED:-?}). Лог: $LOG_DIR/$STAMP.log
$(grep -v '^== ' "$LOG_DIR/$STAMP.log" | tail -5)"
  fi
  if ((saved == 0)); then
    printf '%s\n%s\n' "$why" "$DETAIL" >"$LOG_DIR/refusal.txt"
    if ((RUNS >= MAX_RUNS)); then
      handover "$WORK_BR" "stuck" "⚠️ **Агенти не завершили крок за $RUNS прогонів** ($why). Віддаю вам як є: \`$WORK_BR\` ($SAVE_CHECKS); лікар шукає причину."
    fi
    echo "$WORK_BR $RUNS $FIRST" >"$WORK"
    finish 4 "unfinished" "⏳ **Агенти: крок не завершено** ($why). Роботу збережено в \`$WORK_BR\`; наступний прогін продовжить її сам (прогін $((RUNS + 1)) з $MAX_RUNS), вам нічого робити не треба." --silent
  fi
fi # nothing worth saving: report as below
if ((rc != 0)); then
  finish 5 "error" "❌ **Агенти: claude завершився з помилкою** (код $rc), змін немає; лікар шукає причину. Кінець виводу:
$(tail -c 800 "$SUMMARY")"
else
  finish 3 "idle" "💤 **Агенти: роботи немає.** $(head -c 600 "$SUMMARY")" --silent
fi

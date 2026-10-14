#!/bin/bash
# Deliver the unfinished work left in the agents' copy (the coordinator ended before delivery) as an agent/* branch,
# so the next step does not stash it away. From the Mac:
#   ssh tradedesk@SERVER 'bash -s' < scripts/server/agents-rescue.sh [slug]
# Only project paths (docs, src, tests, scripts) under 2 MB are delivered; anything else stops the script.
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
SLUG="${1:-put-skew-research}"
BR="agent/$(date +%F)-$SLUG"
WT="$HOME/tradedesk-agents"
LOCK="${XDG_STATE_HOME:-$HOME/.local/state}/tradedesk-agents/.lock"

mkdir "$LOCK" 2>/dev/null || { echo "an agent step is running now; try again after it ends"; exit 1; }
trap 'rmdir "$LOCK"' EXIT
cd "$WT"
echo "== unfinished work in $WT"
git status --short --untracked-files=all
[[ -n "$(git status --porcelain)" ]] || { echo "nothing to rescue"; exit 0; }

bad=0
while IFS= read -r path; do
  if [[ ! "$path" =~ ^(docs|src|tests|scripts)/ ]]; then echo "outside project paths: $path"; bad=1; fi
  if [[ -f "$path" && $(stat -c %s "$path") -gt 2000000 ]]; then echo "too big: $path"; bad=1; fi
done < <(git status --porcelain --untracked-files=all | sed -E 's/^.. //; s/.* -> //')
(( bad == 0 )) || { echo "stopped: nothing committed"; exit 1; }

checks="lint and tests pass"
make lint >/tmp/rescue-lint.$$ 2>&1 || checks="LINT FAILS"
make test >/tmp/rescue-test.$$ 2>&1 || checks="$checks; TESTS FAIL"
tail -2 /tmp/rescue-lint.$$ /tmp/rescue-test.$$
rm -f /tmp/rescue-lint.$$ /tmp/rescue-test.$$

git switch -q -c "$BR"
git add -A docs src tests scripts
git commit -q -F - <<EOF
Unfinished agent step: $SLUG ($checks)

The coordinator ended its run while the quant was on the fix round, so
the launcher delivered nothing. This branch keeps that work as it was
left: the quant's research and the critic's review (request changes);
the fix round may be partial. Not ready to merge as a final result.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
git push -q origin "$BR"
echo "delivered $BR ($checks)"
echo "https://github.com/nick198922/MCP/compare/main...$BR?expand=1"

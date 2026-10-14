#!/bin/bash
# For the agents in headless runs, where sleep loops and the Monitor tool are not available: wait for a long
# background job (a data fetch) without ending the turn, which would stop the job.
#   scripts/agents_wait.sh SECONDS [FILE PATTERN]
# Returns after SECONDS (at most 540, under the 10-minute tool timeout) or as soon as FILE has a line matching the
# extended regex PATTERN, then prints the last lines of FILE. Call it again to keep waiting.
set -uo pipefail
max="${1:-300}"
[[ "$max" =~ ^[0-9]+$ ]] || max=300
((max > 540)) && max=540
file="${2:-}"
pattern="${3:-}"
end=$((SECONDS + max))
while ((SECONDS < end)); do
  if [[ -n "$file" && -n "$pattern" ]] && grep -Eq -- "$pattern" "$file" 2>/dev/null; then
    echo "matched: $pattern"
    break
  fi
  sleep 10
done
[[ -n "$file" ]] && tail -5 "$file" 2>/dev/null
exit 0

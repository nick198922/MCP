# Sourced by scripts/nightly_agents.sh and scripts/agents_doctor.sh. Delivery is done here, not by the model.
# deliver <summary file> <branch regex> [work branch]: turns what the model staged into a branch and pushes it, but
# only a branch name matching the regex and new on origin, only with nothing unstaged, only after lint and tests pass.
# With a work branch (the agents' unfinished work, checked out), the commit goes on top of it and the push updates it,
# whatever name the model asked for. Sets DELIVERED (the branch) and DELIVERED_URL (a compare link) on success,
# NOTE (the reason, in Ukrainian) and DETAIL (what to fix: failing lint/test output or unstaged files) otherwise.
# open_pr turns a delivered branch into a pull request.
DELIVERED=""
DELIVERED_URL=""
NOTE=""
DETAIL=""

repo_slug() { git remote get-url origin | sed -E 's#^(git@github.com:|https://github.com/)##; s#\.git$##'; }

# The agents' and the doctor's clones are made by the launcher, and the server user has no global git identity,
# so without this every commit there fails with "Author identity unknown". Call it inside the clone.
ensure_git_identity() {
  git config user.email >/dev/null 2>&1 && return 0
  git config user.name "Mykola Khanetskyi (agents)"
  git config user.email "$(git -C "$REPO" config user.email 2>/dev/null || echo agents@localhost)"
}

# Merge the newest main into the branch before it is pushed, so its pull request merges cleanly. CHANGELOG.md, which
# everyone appends to at the top, is merged by keeping both sides; any other conflict is left for the owner's review.
catch_up_with_main() {
  grep -qx 'CHANGELOG.md merge=union' .git/info/attributes 2>/dev/null || echo 'CHANGELOG.md merge=union' >>.git/info/attributes
  git fetch -q origin main || return 0
  git merge -q --no-edit origin/main >/dev/null 2>&1 || { git merge --abort 2>/dev/null; echo "main does not merge cleanly; left for the owner"; }
}

deliver() {
  local summary="$1" pattern="$2" work="${3:-}" br
  if [[ ! -f .agent/branch || ! -f .agent/commit-msg.txt ]]; then
    NOTE="доставку не запрошено"
    return 0
  fi
  br="$(head -1 .agent/branch | tr -d '[:space:]')"
  [[ -n "$work" ]] && br="$work"
  if [[ ! "$br" =~ $pattern ]]; then
    NOTE="неприпустима назва гілки '$br'"
    return 1
  fi
  if [[ "$br" != "$work" ]] && git ls-remote --exit-code --heads origin "$br" >/dev/null 2>&1; then
    NOTE="гілка $br уже є на GitHub"
    return 1
  fi
  if git diff --cached --quiet; then
    NOTE="нічого не проіндексовано"
    return 1
  fi
  if ! git diff --quiet; then
    NOTE="є непроіндексовані зміни у відстежуваних файлах"
    DETAIL="Unstaged changes in tracked files (stage them with git add, or leave them out of the step only if they do not belong to it):
$(git diff --name-only)"
    return 1
  fi
  local checks
  checks="$(make lint 2>&1 && make test 2>&1)"
  local ok=$?
  echo "$checks"
  if ((ok != 0)); then
    NOTE="lint або тести не пройшли"
    DETAIL="make lint && make test failed; the end of the output:
$(tail -40 <<<"$checks")"
    return 1
  fi
  # errexit is off inside a function called from `if` or `||`, so every step is checked explicitly.
  # -C: a local branch of this name can only be left by an earlier attempt that failed before pushing.
  git switch -C "$br" || return 1
  git commit -q -F .agent/commit-msg.txt || return 1
  catch_up_with_main
  git push -q -u origin "$br" || return 1
  DELIVERED="$br"
  DELIVERED_URL="https://github.com/$(repo_slug)/compare/main...$br?expand=1"
  echo "delivered $br"
  printf '\n**Гілка:** `%s`\n' "$br" >>"$summary"
  git checkout --quiet --detach origin/main
}

# open_pr <branch> <title> <body file> [--ready]: prints the URL of the branch's pull request, opened by
# scripts/gh_pr.py with the token in ~/.config/tradedesk/github.env; the compare link if there is no token or GitHub
# refuses. --ready lets .github/workflows/automerge.yml (or scripts/pr_watch.py) merge it once CI passes.
open_pr() {
  python3 "$REPO/scripts/gh_pr.py" --repo "$(repo_slug)" --head "$1" --title "$2" --body-file "$3" ${4:+"$4"} ||
    echo "https://github.com/$(repo_slug)/compare/main...$1?expand=1"
}

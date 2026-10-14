# Sourced by scripts/nightly_agents.sh and scripts/agents_doctor.sh. Delivery is done here, not by the model.
# deliver <summary file> <branch regex>: turns what the model staged into a branch and pushes it, but only a
# branch name matching the regex and new on origin, only with nothing unstaged, only after lint and tests pass.
# Sets DELIVERED (the branch) and DELIVERED_URL on success, NOTE (the reason, in Ukrainian) and DETAIL (what to fix:
# failing lint/test output or unstaged files) otherwise.
DELIVERED=""
DELIVERED_URL=""
NOTE=""
DETAIL=""

repo_slug() { git remote get-url origin | sed -E 's#^(git@github.com:|https://github.com/)##; s#\.git$##'; }

deliver() {
  local summary="$1" pattern="$2" br url
  if [[ ! -f .agent/branch || ! -f .agent/commit-msg.txt ]]; then
    NOTE="доставку не запрошено"
    return 0
  fi
  br="$(head -1 .agent/branch | tr -d '[:space:]')"
  if [[ ! "$br" =~ $pattern ]]; then
    NOTE="неприпустима назва гілки '$br'"
    return 1
  fi
  if git ls-remote --exit-code --heads origin "$br" >/dev/null 2>&1; then
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
  git switch -c "$br" || return 1
  git commit -q -F .agent/commit-msg.txt || return 1
  git push -q -u origin "$br" || return 1
  url="https://github.com/$(repo_slug)/compare/main...$br?expand=1"
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    url="$(gh pr create --base main --head "$br" --title "$(head -1 .agent/commit-msg.txt)" --body-file .agent/commit-msg.txt)" || true
  fi
  DELIVERED="$br"
  DELIVERED_URL="$url"
  echo "delivered $br: $url"
  printf '\n**Гілка:** `%s`\n**PR:** %s\n' "$br" "$url" >>"$summary"
  git checkout --quiet --detach origin/main
}

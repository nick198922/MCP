#!/bin/bash
# Step 3 of the server move: uv, Claude Code and a GitHub deploy key for the tradedesk user.
#   ssh tradedesk@SERVER 'bash -s' < scripts/server/03-tradedesk-tools.sh
set -euo pipefail
cd "$HOME"
export PATH="$HOME/.local/bin:$PATH"

echo "== uv"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version

echo "== Claude Code"
command -v claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash
claude --version
grep -q '.local/bin' "$HOME/.bashrc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >>"$HOME/.bashrc"

echo "== GitHub host key (checked against GitHub's published ED25519 fingerprint)"
expected="SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
key="$(ssh-keyscan -t ed25519 github.com 2>/dev/null)"
got="$(echo "$key" | ssh-keygen -lf - | awk '{print $2}')"
if [[ "$got" != "$expected" ]]; then
  echo "github.com host key fingerprint $got does not match $expected; stopping"
  exit 1
fi
touch "$HOME/.ssh/known_hosts"
grep -qF "$key" "$HOME/.ssh/known_hosts" || echo "$key" >>"$HOME/.ssh/known_hosts"

echo "== deploy key"
[[ -f "$HOME/.ssh/github_deploy" ]] || ssh-keygen -q -t ed25519 -N "" -C "tradedesk-server deploy key" -f "$HOME/.ssh/github_deploy"
cat >"$HOME/.ssh/config" <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
chmod 600 "$HOME/.ssh/config"
echo
echo "Add this key on GitHub: repo nick198922/MCP -> Settings -> Deploy keys -> Add deploy key, tick 'Allow write access':"
echo
cat "$HOME/.ssh/github_deploy.pub"

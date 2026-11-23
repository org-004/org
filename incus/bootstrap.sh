#!/bin/sh
# bootstrap.sh: what an engineer's container starts with. `org up` runs this once, as root,
# inside the template container; every engineer is then a copy of the template.
#
# It installs tools and writes settings. It puts no login anywhere: the template is blank, and
# each run's tokens are copied into that run's container, in memory, at dispatch.
#
# Prints facts as key=value on stdout; progress goes to stderr.
set -u
export DEBIAN_FRONTEND=noninteractive
export PATH="$HOME/.local/bin:$PATH"

say() { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

need=""
for c in python3 git gh tmux curl bwrap socat rg jq docker; do have "$c" || need="$need $c"; done
if [ -n "$need" ]; then
  note "installing tools (missing:$need)"
  apt-get update -qq >/dev/null 2>&1 || note "bootstrap: apt-get update did not finish"
  apt-get install -y -qq --no-install-recommends \
    ca-certificates python3 python3-venv git gh tmux curl bubblewrap socat ripgrep jq less procps unzip xz-utils file \
    build-essential pkg-config openssh-client iproute2 docker.io docker-cli >/dev/null 2>&1 || note "bootstrap: apt-get install did not finish"
  # The build and compose plugins are separate packages; a project's tests often want both.
  apt-get install -y -qq --no-install-recommends docker-buildx docker-compose >/dev/null 2>&1 || note "bootstrap: docker plugins not installed"
fi
if have docker && have systemctl; then systemctl enable --now docker >/dev/null 2>&1 || true; fi

if ! have claude; then
  note "installing Claude Code"
  curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1 || note "bootstrap: Claude Code did not install"
fi

# Claude Code's settings for an engineer: no attribution lines, no memory carried between
# engineers, and no MCP servers or claude.ai connectors. An engineer is root here and could
# undo the last part; what keeps it from the person's accounts is that the token it is given
# can only make model requests.
mkdir -p "$HOME/.claude" "$HOME/.local/bin" /etc/claude-code
cat > "$HOME/.claude/settings.json" <<'JSON'
{
  "attribution": {
    "commit": "",
    "pr": "",
    "sessionUrl": false
  },
  "autoMemoryEnabled": false,
  "skipDangerousModePermissionPrompt": true,
  "disableClaudeAiConnectors": true
}
JSON
printf '{"mcpServers": {}}\n' > /etc/claude-code/managed-mcp.json
printf '{"disableClaudeAiConnectors": true}\n' > /etc/claude-code/managed-settings.json

# git asks gh for a password, and gh reads GH_TOKEN, which the runner sets for its session.
git config --global --replace-all credential.https://github.com.helper '' 2>/dev/null
git config --global --add credential.https://github.com.helper '!gh auth git-credential' 2>/dev/null
git config --global init.defaultBranch main 2>/dev/null

# A copy must not come up as the same machine: the id seeds the address it asks for.
: > /etc/machine-id
rm -f /var/lib/dbus/machine-id
apt-get clean >/dev/null 2>&1 || true

left=""
for c in python3 git gh tmux curl claude; do have "$c" || left="$left $c"; done
say "missing=$(printf '%s' "$left" | sed 's/^ //')"
say "docker=$(have docker && docker info >/dev/null 2>&1 && echo yes || echo no)"
say "claude=$(claude --version 2>/dev/null | head -1)"
say "login=$([ -e "$HOME/.claude/.credentials.json" ] || [ -d "$HOME/.config/gh" ] && echo yes || echo no)"

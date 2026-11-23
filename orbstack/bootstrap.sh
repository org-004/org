#!/bin/bash
# Runs inside the template machine, as its default user, fed over stdin by `org up`:
#
#   orb -m org-base bash -s -- [--no-install] < orbstack/bootstrap.sh
#
# It installs what an engineer's machine needs and the template lacks, and nothing else, so a
# template cloned from a machine that is already set up is left as it is. Engineers and the
# manager are clones of the template and inherit all of it.
#
#   python3 git gh tmux curl     the runner, and what an engineer works with
#   bubblewrap socat             Claude Code's shell sandbox, which confines the manager chat
#   docker                       a daemon of the machine's own, so an engineer can build and run containers
#   claude                       Claude Code, in ~/.local/bin
#
# --no-install reports what is missing and installs nothing (the tests use it).
# What it prints on stdout are `name=value` facts that `org up` reads; progress goes to stderr.
set -u

install=1
[ "${1:-}" = "--no-install" ] && install=""

say() { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*" >&2; }  # progress, on stderr; stdout carries only the facts
have() { command -v "$1" >/dev/null 2>&1; }
export PATH="$HOME/.local/bin:$PATH"

missing() {
  out=""
  for c in python3 git gh tmux curl bwrap socat; do have "$c" || out="$out $c"; done
  printf '%s' "$out"
}

# command -> package, for each package manager this knows
pkg_for() {
  case "$1:$2" in
    apt:bwrap|dnf:bwrap|pacman:bwrap|apk:bwrap) echo bubblewrap ;;
    pacman:python3) echo python ;;
    pacman:gh|apk:gh) echo github-cli ;;
    *) echo "$2" ;;
  esac
}

if [ -n "$install" ]; then
  need=$(missing)
  mgr=""
  for m in apt-get pacman dnf apk; do have "$m" && { mgr="${m%-get}"; break; }; done
  if [ -n "$need" ] || ! have docker; then
    [ -n "$mgr" ] || { note "bootstrap: no package manager this script knows (apt, pacman, dnf, apk); install by hand:$need docker"; }
    pkgs=""
    for c in $need; do pkgs="$pkgs $(pkg_for "$mgr" "$c")"; done
    case "$mgr" in
      apt)
        export DEBIAN_FRONTEND=noninteractive
        note "installing:$pkgs (and what a build usually wants)"
        sudo -E apt-get update -q >/dev/null \
          && sudo -E apt-get install -y -q --no-install-recommends ca-certificates $pkgs \
               ripgrep jq less procps unzip xz-utils file build-essential pkg-config openssh-client >/dev/null \
          || note "bootstrap: apt-get did not finish"
        ;;
      pacman)
        # No -y by itself: syncing the database without upgrading leaves a machine half upgraded.
        note "installing:$pkgs"
        sudo pacman -S --noconfirm --needed $pkgs ripgrep jq unzip base-devel openssh >/dev/null 2>&1 \
          || sudo pacman -Syu --noconfirm --needed $pkgs ripgrep jq unzip base-devel openssh >/dev/null \
          || note "bootstrap: pacman did not finish"
        ;;
      dnf)
        note "installing:$pkgs"
        sudo dnf install -y -q $pkgs ripgrep jq unzip xz file gcc gcc-c++ make pkgconf-pkg-config openssh-clients >/dev/null \
          || note "bootstrap: dnf did not finish"
        ;;
      apk)
        note "installing:$pkgs"
        sudo apk add -q $pkgs bash ripgrep jq unzip xz file build-base openssh-client >/dev/null || note "bootstrap: apk did not finish"
        ;;
    esac
  fi

  # A Docker daemon of the machine's own. A machine is a whole Linux system, so this is the
  # ordinary install; the user joins the docker group so that no engineer needs sudo for it.
  if ! have docker; then
    note "installing: docker"
    case "$mgr" in
      pacman) sudo pacman -S --noconfirm --needed docker docker-buildx docker-compose >/dev/null 2>&1 || note "bootstrap: docker did not install" ;;
      apk)    sudo apk add -q docker docker-cli-buildx docker-cli-compose >/dev/null || note "bootstrap: docker did not install" ;;
      *)      curl -fsSL https://get.docker.com | sudo sh >/dev/null 2>&1 \
                || { [ "$mgr" = apt ] && sudo -E apt-get install -y -q docker.io >/dev/null; } \
                || note "bootstrap: docker did not install" ;;
    esac
  fi
  if have docker; then
    if have systemctl; then sudo systemctl enable --now docker >/dev/null 2>&1 || true; fi
    if have rc-update; then sudo rc-update add docker default >/dev/null 2>&1; sudo rc-service docker start >/dev/null 2>&1 || true; fi
    id -nG | tr ' ' '\n' | grep -qx docker || sudo usermod -aG docker "$(id -un)" 2>/dev/null || sudo addgroup "$(id -un)" docker 2>/dev/null || true
  fi

  if ! have claude; then
    note "installing: Claude Code"
    curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1 || note "bootstrap: Claude Code did not install"
  fi
fi

# Claude Code's settings for a machine org made: no attribution lines, no memory carried between
# engineers. A settings file that is already there belongs to whoever set the machine up.
mkdir -p "$HOME/.claude" "$HOME/.local/bin"
if [ ! -e "$HOME/.claude/settings.json" ]; then
  cat > "$HOME/.claude/settings.json" <<'JSON'
{
  "attribution": {
    "commit": "",
    "pr": "",
    "sessionUrl": false
  },
  "autoMemoryEnabled": false,
  "skipDangerousModePermissionPrompt": true
}
JSON
fi

# ---- facts, for `org up`
left=$(missing)
have claude || left="$left claude"
say "missing=$(printf '%s' "$left" | sed 's/^ //')"
# The group was added a moment ago and this shell predates it, so ask the daemon as root too.
if have docker && { docker info >/dev/null 2>&1 || { [ -n "$install" ] && sudo -n docker info >/dev/null 2>&1; }; }; then
  say "docker=yes"
else
  say "docker=no"
fi
say "mac_files=$([ -n "$(ls -A /mnt/mac 2>/dev/null)" ] && echo yes || echo no)"
say "claude=$(claude --version 2>/dev/null | head -1)"
say "github=$(gh auth token >/dev/null 2>&1 && echo yes || echo no)"
say "git_name=$(git config --global user.name 2>/dev/null || true)"
say "git_email=$(git config --global user.email 2>/dev/null || true)"
say "token=$([ -s "$HOME/.claude/oauth-token" ] && echo yes || echo no)"

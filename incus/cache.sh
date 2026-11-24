#!/bin/sh
# cache.sh: what the build cache's container runs. `org up` runs this once, as root, inside the
# cache container (org-cache) when it makes it. Run a second time, it changes nothing it need not.
#
# It installs bazel-remote 2.6.4, pinned by its SHA-256, and runs it as a service of its own user
# that serves Bazel's remote cache over gRPC on one port.
# The container holds no login, no token and no key, and this puts none there.
# The server asks for no credential: what protects it is that only containers on the VM's bridge
# can reach it.
#
# ORG_CACHE_PORT (default 9092) is the gRPC port; ORG_CACHE_MAX_GB (default 20) is how many GiB
# the cache may keep before it forgets the oldest results.
#
# Prints facts as key=value on stdout; progress goes to stderr.
set -u
export DEBIAN_FRONTEND=noninteractive

say() { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
refuse() { note "cache: $*"; say "server=down"; exit 1; }

version=2.6.4
bin=/usr/local/bin/bazel-remote
dir=/var/lib/bazel-remote
unit=/etc/systemd/system/bazel-remote.service
port="${ORG_CACHE_PORT:-9092}"
max_gb="${ORG_CACHE_MAX_GB:-20}"

say "version=$version"
# Both numbers are written into the service's command line, so nothing but digits may get there.
case "$port" in ''|*[!0-9]*|0*) refuse "ORG_CACHE_PORT must be a positive integer, not '$port'" ;; esac
[ "${#port}" -le 5 ] && [ "$port" -le 65535 ] || refuse "ORG_CACHE_PORT $port is not a port"
case "$max_gb" in ''|*[!0-9]*|0*) refuse "ORG_CACHE_MAX_GB must be a positive integer, not '$max_gb'" ;; esac

case "$(uname -m)" in
  aarch64|arm64) arch=arm64; sum=295e54fd58841211b00fb204cafc13affc6a13d8946a4520c0ecdce22626ef93 ;;
  x86_64|amd64) arch=amd64; sum=6a6b64666c2238ec6b49b7b29f5e41e4149de09417a0028897c9bffe40376876 ;;
  *) refuse "no pinned bazel-remote for the architecture $(uname -m)" ;;
esac
say "arch=$arch"

if ! have curl; then
  note "installing curl"
  apt-get update -qq >/dev/null 2>&1 || note "cache: apt-get update did not finish"
  apt-get install -y -qq --no-install-recommends ca-certificates curl >/dev/null 2>&1 || note "cache: apt-get install did not finish"
  have curl || refuse "curl is missing and did not install"
fi

# The download is only trusted when it is, byte for byte, the release whose hash is written above.
if [ "$(sha256sum "$bin" 2>/dev/null | cut -d' ' -f1)" != "$sum" ]; then
  note "downloading bazel-remote $version ($arch)"
  url="https://github.com/buchgr/bazel-remote/releases/download/v$version/bazel-remote-$version-linux-$arch"
  mkdir -p "$(dirname "$bin")"
  curl -fsSL --retry 3 --max-time 300 -o "$bin.download" "$url" || { rm -f "$bin.download"; refuse "the download from $url failed"; }
  if [ "$(sha256sum "$bin.download" | cut -d' ' -f1)" != "$sum" ]; then
    rm -f "$bin.download"
    note "cache: the download's SHA-256 is not the pinned one, so it was removed and nothing was installed"
    say "sha256=mismatch"
    say "server=down"
    exit 1
  fi
  chmod 755 "$bin.download" && mv -f "$bin.download" "$bin" || { rm -f "$bin.download"; refuse "the download could not be put at $bin"; }
fi
say "sha256=ok"

# The server needs no root: a user of its own that cannot log in, and one directory it owns.
if ! id bazel-remote >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$dir" --no-create-home --shell /usr/sbin/nologin bazel-remote \
    || refuse "the user bazel-remote could not be made"
fi
mkdir -p "$dir" && chown bazel-remote:bazel-remote "$dir" && chmod 700 "$dir" || refuse "$dir could not be made"
say "user=bazel-remote"
say "dir=$dir"
say "port=$port"
say "max_gb=$max_gb"

# The one place the server's command line is written; the live test reads it from here too.
# HTTP is bound to the container's own loopback, so the gRPC port is all other containers reach.
# Without --access_log_level none, bazel-remote logs a line for every request, and every build
# makes hundreds, so the journal would grow with each build.
# No ProtectSystem, PrivateTmp or other sandboxing: those need namespaces that an unprivileged
# container nested in another one may not be allowed to make, and the service would not start.
cat > "$unit" <<UNIT
[Unit]
Description=bazel-remote, the engineers' shared Bazel build cache
After=network-online.target
Wants=network-online.target

[Service]
User=bazel-remote
Group=bazel-remote
ExecStart=$bin --dir $dir --max_size $max_gb --grpc_address 0.0.0.0:$port --http_address 127.0.0.1:8080 --access_log_level none
Restart=on-failure
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
UNIT

if [ ! -d /run/systemd/system ]; then
  # A container without a service manager (Docker, in the live test) gets everything but a running server.
  say "service=none"
  say "server=down"
  exit 0
fi
say "service=systemd"
systemctl daemon-reload >/dev/null 2>&1 || note "cache: systemctl daemon-reload failed"
systemctl enable bazel-remote.service >/dev/null 2>&1 || note "cache: the service could not be enabled"
# Restart, not start: a run with another port or size must replace a server that already runs.
systemctl restart bazel-remote.service >/dev/null 2>&1 || note "cache: the service did not start"

# An active unit is not yet a server. It answers when its HTTP status page does and something
# takes an HTTP/2 connection on the gRPC port; curl does both, and curl is in every image this
# runs in, where ss (iproute2) is not.
up=down
i=0
while [ "$i" -lt 30 ]; do
  if curl -fsS -o /dev/null -m 2 http://127.0.0.1:8080/status 2>/dev/null \
    && curl -sS -o /dev/null -m 2 --http2-prior-knowledge "http://127.0.0.1:$port/" 2>/dev/null; then
    up=up
    break
  fi
  i=$((i + 1))
  sleep 1
done
[ "$up" = up ] || note "cache: bazel-remote did not answer within 30 s (journalctl -u bazel-remote)"
say "server=$up"

# One image for both roles: the manager and every engineer.
#
#   org up        builds this and starts the manager from it   (docker/up.sh)
#   org approve   starts one container per run from it         (org/dispatch.py)
#
# The difference between the roles is what the container is given, not what is in the image:
# the manager gets the Docker socket, the ledger volume and the login tokens; an engineer gets
# none of those, its own network, and sudo.
FROM debian:trixie-slim

ARG DOCKER_CLI_VERSION=29.10.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git gh python3 tmux sudo openssh-client \
      ripgrep jq less procps unzip xz-utils file tzdata \
      build-essential pkg-config \
      bubblewrap socat \
 && rm -rf /var/lib/apt/lists/*

# The Docker client only: the manager drives the host's daemon through the mounted socket.
RUN curl -fsSL "https://download.docker.com/linux/static/stable/$(uname -m)/docker-${DOCKER_CLI_VERSION}.tgz" \
  | tar -xz -C /usr/local/bin --strip-components=1 docker/docker

# One user for both roles. An engineer owns its machine, so it has sudo; the manager container is
# started with no-new-privileges, which makes that sudo inert there.
RUN useradd --create-home --shell /bin/bash --uid 1000 org \
 && echo 'org ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/org \
 && chmod 440 /etc/sudoers.d/org

USER org
WORKDIR /home/org
# Everything Claude Code keeps (settings, login token, conversations) under one directory, so the
# manager can keep it on a volume and its conversations survive a rebuild.
ENV CLAUDE_CONFIG_DIR=/home/org/.claude \
    PATH=/home/org/.local/bin:/opt/org/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    ORG_IN_CONTAINER=1 \
    ORG_USE_TOKEN_FILE=1 \
    DISABLE_AUTOUPDATER=1 \
    LANG=C.UTF-8

RUN curl -fsSL https://claude.ai/install.sh | bash \
 && claude --version \
 # Mount points for the manager's volumes, owned by the user, so the volumes are too.
 && mkdir -p /home/org/.org /home/org/.config/gh /home/org/.claude /home/org/.ssh && chmod 700 /home/org/.ssh

COPY --chown=org:org docker/claude-settings.json /home/org/.claude/settings.json
COPY --chown=org:org . /opt/org

CMD ["sleep", "infinity"]

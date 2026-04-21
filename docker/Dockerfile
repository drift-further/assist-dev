ARG NODE_VERSION=20
FROM node:${NODE_VERSION}-bookworm

ARG PYTHON_VERSION=3
ARG CLAUDE_VERSION=latest
ARG HOST_UID=1000
ARG HOST_GID=1000
ARG PIP_PACKAGES=""
ARG SYSTEM_PACKAGES=""

# Core system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget jq unzip zip xz-utils \
    python${PYTHON_VERSION} python${PYTHON_VERSION}-pip python${PYTHON_VERSION}-venv \
    ripgrep fd-find \
    xvfb x11vnc fluxbox \
    chromium firefox-esr \
    libgtk-3-0 libx11-xcb1 libxcomposite1 libxcursor1 libxdamage1 \
    libxi6 libxtst6 libnss3 libxss1 libxrandr2 libasound2 \
    libpangocairo-1.0-0 libatk1.0-0 libcairo-gobject2 libgdk-pixbuf2.0-0 \
    postgresql-client sudo \
    && apt-get clean

# Security: No Docker in container
RUN apt-get purge -y docker docker-engine docker.io containerd runc 2>/dev/null || true

# Python aliases
RUN ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python && \
    ln -sf /usr/bin/pip${PYTHON_VERSION} /usr/bin/pip 2>/dev/null || true

# Base Python packages
RUN pip install --break-system-packages psycopg2-binary fastapi uvicorn httpx beautifulsoup4 pyyaml

# Additional global pip packages (from container config)
RUN if [ -n "$PIP_PACKAGES" ]; then pip install --break-system-packages $PIP_PACKAGES; fi

# Additional system packages (from container config)
RUN if [ -n "$SYSTEM_PACKAGES" ]; then apt-get update && apt-get install -y $SYSTEM_PACKAGES && apt-get clean; fi

# Workspace
RUN mkdir -p /workspace
WORKDIR /workspace

# Copy scripts
COPY entrypoint.sh /entrypoint.sh
COPY scripts/link-agents.sh /link-agents.sh
COPY scripts/claude-statusline.sh /usr/local/bin/claude-statusline
RUN chmod +x /entrypoint.sh /link-agents.sh /usr/local/bin/claude-statusline

# Developer user (matches host UID/GID — remove existing UID 1000 if taken by base image)
RUN id -u ${HOST_UID} &>/dev/null && userdel -r $(id -nu ${HOST_UID}) || true && \
    groupadd -g ${HOST_GID} developer 2>/dev/null || true && \
    useradd -m -u ${HOST_UID} -g ${HOST_GID} -s /bin/bash developer

# Security: sudo with denylist
RUN echo 'developer ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/developer && \
    chmod 0440 /etc/sudoers.d/developer

# Claude Code installation (native installer — must run as developer user)
ARG CACHEBUST_CLAUDE=1
USER developer
RUN curl -fsSL https://claude.ai/install.sh | bash
USER root
ENV PATH="/home/developer/.local/bin:${PATH}"

# Extensions (generated at build time from enabled extensions)
COPY extensions-install.sh /tmp/extensions-install.sh
RUN chmod +x /tmp/extensions-install.sh && /tmp/extensions-install.sh

# Mount points
RUN mkdir -p /host-flutter /host-claude-config /host-claude-home

# Fix ownership
RUN chown -R developer:developer /home/developer

ENTRYPOINT ["/entrypoint.sh"]

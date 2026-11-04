# =============================================================================
# Hermes Assistant — multi-stage build
#   Stage 1 (builder): build-essential + uv + pre-bake hermes-agent
#   Stage 2 (runtime): minimal apt, copy artefacts from builder
# Net result: ~200MB smaller than single-stage, hermes-agent pre-installed so
# first start is instant (no 5-10 min download).
# =============================================================================

ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm

# ─── Stage 1 — builder ──────────────────────────────────────────────────────
FROM ${BUILD_FROM} AS builder

# Pinned, not "main". A floating ref means every rebuild seeds a different
# agent version, so two users on the same add-on version get different
# software and a cosmetic upstream change can turn the build red (it did:
# v0.20.1 renamed `hermes --version`'s "Project:" label and broke the CI
# smoke test). Users who want newer can still set auto_update_agent: true,
# which runs `hermes update` against the /data mirror at every boot.
# Bump this deliberately; check https://github.com/NousResearch/hermes-agent/releases
ARG HERMES_AGENT_REF=v0.22.6
ARG TTYD_VERSION=1.7.7

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/usr/local/bin:/usr/bin:/bin

# Build-time apt — wheels usually exist for our deps, but compiler is needed
# for hermes-agent which pulls a larger dep tree (some may need C extensions).
# ripgrep is included so install.sh stops warning about missing rg.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-venv \
        xz-utils \
        build-essential \
        python3-dev \
        libffi-dev \
        libssl-dev \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# ttyd static binary
RUN set -eux; \
    DPKG_ARCH="$(dpkg --print-architecture)"; \
    case "${DPKG_ARCH}" in \
      amd64)  TTYD_ARCH="x86_64"  ;; \
      arm64)  TTYD_ARCH="aarch64" ;; \
      *) echo "unsupported arch: ${DPKG_ARCH}"; exit 1 ;; \
    esac; \
    curl -fsSL -o /opt/ttyd \
      "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${TTYD_ARCH}"; \
    chmod +x /opt/ttyd; \
    /opt/ttyd --version

# uv
RUN curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh \
    && uv --version

# A Webapp-capable, fail-closed seed; Python and Node are explicit image assets.
ENV UV_PYTHON_INSTALL_DIR=/opt/hermes-python
RUN git clone --depth=1 --branch "${HERMES_AGENT_REF}" https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent-code \
    && uv venv /opt/hermes-agent-code/venv --python 3.14 \
    && env UV_PROJECT_ENVIRONMENT=/opt/hermes-agent-code/venv uv sync --project /opt/hermes-agent-code --frozen --no-dev --extra all --extra messaging \
    && mkdir -p /opt/hermes-agent \
    && touch /opt/hermes-agent/.prebaked
RUN set -eu; arch=$(dpkg --print-architecture); \
    case "$arch" in amd64) nodearch=x64 ;; arm64) nodearch=arm64 ;; *) exit 1 ;; esac; \
    curl -fsSL "https://nodejs.org/dist/v22.23.0/node-v22.23.0-linux-${nodearch}.tar.xz" -o /tmp/node.tar.xz; \
    mkdir -p /opt/hermes-agent/node; \
    tar -xJf /tmp/node.tar.xz -C /opt/hermes-agent/node --strip-components=1; \
    rm /tmp/node.tar.xz

# Keep the complete agent checkout for Webapp builds and in-place updates.

# ─── Stage 2 — runtime (minimal) ────────────────────────────────────────────
FROM ${BUILD_FROM}

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HERMES_HOME=/data/hermes \
    PATH=/usr/local/bin:/usr/bin:/bin

# Runtime apt — no compilers, no pip, no tini, no tzdata (base has it).
# Keep: bash/curl/git/jq/python/openssh-client/ffmpeg/ripgrep — used by Hermes.
# nginx-light multiplexes the Webapp + recovery + ttyd behind a single Ingress port.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        bash \
        jq \
        python3 \
        python3-yaml \
        xz-utils \
        openssl \
        libatomic1 \
        openssh-client \
        ffmpeg \
        ripgrep \
        nginx-light \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Bring binaries + venvs from builder
COPY --from=builder /opt/ttyd                 /usr/local/bin/ttyd
COPY --from=builder /usr/local/bin/uv         /usr/local/bin/uv
COPY --from=builder /usr/local/bin/uvx        /usr/local/bin/uvx

COPY --from=builder /opt/hermes-agent         /opt/hermes-agent
COPY --from=builder /opt/hermes-agent-code    /opt/hermes-agent-code
COPY --from=builder /opt/hermes-python        /opt/hermes-python

# PM-aware dispatcher, shared by normal and upstream-patch images.
COPY hermes-launcher.sh /usr/local/bin/hermes
RUN chmod +x /usr/local/bin/hermes

# Add-on entrypoint + nginx multiplex config
COPY run.sh /run.sh
COPY bw-setup.sh /usr/local/bin/bw-setup.sh
COPY bw-shim.sh /usr/local/bin/bw-shim.sh
COPY bw-wrapper.sh /usr/local/bin/bw
COPY bw-session.sh /usr/local/bin/bw-session
RUN env PATH=/opt/hermes-agent/node/bin:/usr/local/bin:/usr/bin:/bin npm install --prefix /opt/bw @bitwarden/cli \
    && chmod +x /usr/local/bin/bw-shim.sh /usr/local/bin/bw-setup.sh /usr/local/bin/bw /usr/local/bin/bw-session
COPY hermes-safe-update.sh /usr/local/bin/hermes-safe-update.sh
COPY webapp-ticket.py /usr/local/bin/webapp-ticket.py
COPY addon_health.py /usr/local/bin/addon_health.py
COPY addon_config.py /usr/local/bin/addon_config.py
COPY addon_runtime.py /usr/local/bin/addon_runtime.py
COPY nginx.conf /etc/nginx/nginx.conf
RUN chmod +x /run.sh /usr/local/bin/hermes-safe-update.sh /usr/local/bin/webapp-ticket.py /usr/local/bin/addon_health.py /usr/local/bin/addon_config.py \
    && mkdir -p /run/nginx /var/log/nginx

EXPOSE 8787 7681

# Gateway/Webapp readiness with bounded startup grace; no classic dependency.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD ["curl", "-fsS", "http://127.0.0.1:8787/health"]

# Defer to HA base s6-overlay ENTRYPOINT — it sets up bashio with-contenv.
CMD ["/run.sh"]

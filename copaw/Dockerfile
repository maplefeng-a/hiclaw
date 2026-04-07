# ============================================================
# HiClaw CoPaw Worker Agent - Python-based Container
# ============================================================
# Lightweight Python container for running CoPaw-based Worker Agents.
# Uses the same MinIO-backed config/sync model as OpenClaw workers.
#
# Two runtime modes, each with its own isolated venv:
#   - Standard mode (HICLAW_CONSOLE_PORT set): copaw from PyPI + copaw-worker
#   - Lite mode (default, no console port):    copaw from PyPI + copaw-worker (with lazy loading patches)
#
# Build args:
#   HIGRESS_REGISTRY  - base image registry for mc (MinIO Client)
#   APT_MIRROR        - Debian mirror (default: mirrors.aliyun.com)
#   PIP_INDEX_URL     - PyPI mirror (default: Aliyun)
#   COPAW_VERSION     - CoPaw version to install from PyPI (default: 1.0.0)
# ============================================================

ARG HIGRESS_REGISTRY=higress-registry.cn-hangzhou.cr.aliyuncs.com

# ============ Stage 1: mc (MinIO Client) ============
FROM ${HIGRESS_REGISTRY}/higress/mc:20260216 AS mc

# ============ Final Image ============
FROM ${HIGRESS_REGISTRY}/higress/python:3.11-slim

ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG COPAW_VERSION=1.0.0

# python:3.11-slim is Debian-based, uses /etc/apt/sources.list.d/debian.sources
RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
        libolm-dev \
        libjemalloc2 \
        curl \
        git \
        jq \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# jemalloc reduces Python memory fragmentation (~10-20% RSS savings).
# Detect the arch-specific .so path so the image works on both amd64 and arm64.
RUN JEMALLOC=$(find /usr/lib -name 'libjemalloc.so.2' -print -quit) \
    && echo "LD_PRELOAD=${JEMALLOC}" >> /etc/environment \
    && echo "${JEMALLOC}" > /etc/ld.so.preload

# mc (MinIO Client) — real binary; wrapper installed after shared libs are copied
COPY --from=mc /usr/bin/mc /usr/local/bin/mc.bin

ARG NPM_REGISTRY=https://registry.npmjs.org/

# Install Node.js 22 for shared Worker CLIs
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

RUN npm config set registry ${NPM_REGISTRY} && \
    npm install -g mcporter skills @nacos-group/cli && \
    rm -rf /root/.npm

# Stage copaw-worker dependency metadata only (src/ copied later for layer caching).
# A minimal __init__.py placeholder lets pip resolve the package without the real source,
# so that dependency-heavy venv layers (~2 GB) are rebuilt only when pyproject.toml changes.
COPY pyproject.toml README.md /tmp/copaw-worker/
RUN mkdir -p /tmp/copaw-worker/src/copaw_worker \
    && echo '__version__ = "0.0.0"' > /tmp/copaw-worker/src/copaw_worker/__init__.py

# --- Standard venv: copaw from PyPI ---
RUN python -m venv /opt/venv/standard \
    && /opt/venv/standard/bin/pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "$(echo ${PIP_INDEX_URL} | sed 's|https\?://||;s|/.*||')" \
        /tmp/copaw-worker/

# --- Lite venv: copaw from PyPI (with lazy loading patches) ---
RUN python -m venv /opt/venv/lite \
    && /opt/venv/lite/bin/pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "$(echo ${PIP_INDEX_URL} | sed 's|https\?://||;s|/.*||')" \
        "copaw==${COPAW_VERSION}" \
    && /opt/venv/lite/bin/pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "$(echo ${PIP_INDEX_URL} | sed 's|https\?://||;s|/.*||')" \
        /tmp/copaw-worker/

# Add venv bin to PATH so copaw CLI is available (matches manager-copaw pattern)
ENV PATH="/opt/venv/lite/bin:${PATH}"

# ---------------------------------------------------------------------------
# Patch installed packages in the lite venv to defer heavyweight modules that
# copaw-worker headless mode never uses.  Total saving: ~170 MB RSS.
#
#   patch_reme_lazy.py             — reme: chromadb/pandas/mcp/elasticsearch
#   patch_agentscope_lazy.py       — agentscope: redis/socketio/sqlalchemy/mcp
#   patch_agentscope_runtime_lazy.py — agentscope_runtime: opentelemetry/a2a/fastapi
# ---------------------------------------------------------------------------
COPY scripts/patch_reme_lazy.py \
     scripts/patch_agentscope_lazy.py \
     scripts/patch_agentscope_runtime_lazy.py \
     /tmp/

RUN SITE=/opt/venv/lite/lib/python3.11/site-packages \
    && /opt/venv/lite/bin/python /tmp/patch_reme_lazy.py "$SITE/reme" \
    && /opt/venv/lite/bin/python /tmp/patch_agentscope_lazy.py "$SITE/agentscope" \
    && /opt/venv/lite/bin/python /tmp/patch_agentscope_runtime_lazy.py "$SITE/agentscope_runtime" \
    && rm /tmp/patch_*_lazy.py

# --- Overlay real copaw-worker source (high-frequency changes, ~128KB) ---
# This layer is rebuilt on every src/ change but does NOT invalidate the
# heavy venv layers above, saving ~2 GB of re-download on typical upgrades.
COPY src/ /tmp/copaw-worker/src/
RUN STANDARD_SITE=/opt/venv/standard/lib/python3.11/site-packages/copaw_worker \
    && LITE_SITE=/opt/venv/lite/lib/python3.11/site-packages/copaw_worker \
    && cp -a /tmp/copaw-worker/src/copaw_worker/* "$STANDARD_SITE/" \
    && cp -a /tmp/copaw-worker/src/copaw_worker/* "$LITE_SITE/" \
    && rm -rf /tmp/copaw-worker/

# copaw-sync wrapper
RUN printf '#!/bin/bash\nexec python3 "/root/.copaw-worker/${HICLAW_WORKER_NAME}/skills/file-sync/scripts/copaw-sync.py" "$@"\n' \
    > /usr/local/bin/copaw-sync && chmod +x /usr/local/bin/copaw-sync

# Entrypoint script
COPY --chmod=755 scripts/copaw-worker-entrypoint.sh /opt/hiclaw/scripts/copaw-worker-entrypoint.sh

# Shared environment bootstrap and credential management (hiclaw-env.sh, oss-credentials.sh)
COPY --from=shared . /opt/hiclaw/scripts/lib/

# mc wrapper: auto-refreshes STS credentials before every mc invocation (no-op in local mode)
RUN chmod +x /opt/hiclaw/scripts/lib/mc-wrapper.sh && \
    ln -sf /opt/hiclaw/scripts/lib/mc-wrapper.sh /usr/local/bin/mc

# Create workspace directories for both modes
RUN mkdir -p /root/.copaw-worker

WORKDIR /root/.copaw-worker

ENTRYPOINT ["/opt/hiclaw/scripts/copaw-worker-entrypoint.sh"]

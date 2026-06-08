# syntax=docker/dockerfile:1
#
# VanDorial / GenCall container image — multi-stage build for RHEL-family hosts.
#
# Build context is the REPO ROOT (the frontend lives at ../frontend relative to
# this file), so build with:
#
#     podman build -f gencall/Dockerfile -t vandorial/gencall:latest .
#     docker build  -f gencall/Dockerfile -t vandorial/gencall:latest .
#
# Stage 1 (node:20)            builds the React NOC console.
# Stage 2 (ubi9/python-311)    RHEL-compatible runtime that installs the package
#                              and serves the console + API, runs SIPp.
#
# The same image runs in two modes (see docker-compose.yml):
#   - worker     (default)   : gencall-server  → REST/WS/SIPp engine
#   - controller (Phase 2)   : fleet control plane (see compose `controller` svc)

# ──────────────────────────────────────────────────────────────────────────────
# Stage 1 — Frontend build
# ──────────────────────────────────────────────────────────────────────────────
FROM node:20 AS frontend

WORKDIR /build/frontend

# Install deps first (cached unless package manifests change).
# `npm ci` requires package-lock.json and gives reproducible installs.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Copy the rest of the frontend source and build.
# Vite is configured (vite.config.ts) to emit to ../gencall/web/console, so we
# stage the package dir alongside the frontend to keep that relative path valid.
COPY frontend/ ./
RUN npm run build

# After the build, the console lives at /build/gencall/web/console
# (../gencall/web/console relative to the frontend workdir).

# ──────────────────────────────────────────────────────────────────────────────
# Stage 2 — Python runtime (RHEL UBI9)
# ──────────────────────────────────────────────────────────────────────────────
FROM registry.access.redhat.com/ubi9/python-311 AS runtime

LABEL maintainer="VanDorial / GenCall Team" \
      description="VanDorial GenCall - SIP/VoIP Traffic Generator v2.0 (UBI9)" \
      org.opencontainers.image.source="https://github.com/" \
      org.opencontainers.image.base.name="registry.access.redhat.com/ubi9/python-311"

# The UBI python-311 image runs as a non-root user (default UID 1001). We need
# root briefly to install system packages, then drop back.
USER 0

# ── System dependencies ──────────────────────────────────────────────────────
# SIPp is the SIP load-test engine. It is NOT in the base UBI9 repos. We try to
# install it from EPEL (Extra Packages for Enterprise Linux); if the package is
# unavailable for the arch/release, the build MUST NOT fail — we instead leave
# the build prerequisites in place and document how to add SIPp manually.
#
# To build SIPp from source on UBI9 instead (recommended for production), add:
#
#     RUN git clone --depth 1 -b v3.7.3 https://github.com/SIPp/sipp.git /tmp/sipp \
#      && cd /tmp/sipp && cmake . -DUSE_PCAP=1 -DUSE_SSL=1 -DUSE_SCTP=1 \
#      && make -j"$(nproc)" && make install && rm -rf /tmp/sipp
#
# and point [sipp] command at /usr/local/bin/sipp (the default in gencall.cfg).
RUN dnf install -y --nodocs \
        https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm \
    || echo "WARN: could not enable EPEL — SIPp must be installed manually (see comment above)." ; \
    dnf install -y --nodocs \
        libpcap-devel \
        libsctp-devel \
        openssl-devel \
        gcc gcc-c++ make cmake git \
    && ( dnf install -y --nodocs sipp \
         && echo "SIPp installed from EPEL." \
         || echo "WARN: 'sipp' not available via dnf/EPEL on this arch/release. \
Image build continues; install SIPp manually (EPEL package name 'sipp', or build \
from source per the commented recipe above) before running live SIP traffic." ) \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# ── GenCall package ──────────────────────────────────────────────────────────
WORKDIR /opt/gencall

# Install Python deps first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the application source. (.dockerignore strips node_modules, *.db, logs,
# tests, docs, etc., so this is a lean, reproducible copy.)
COPY . .

# Overlay the freshly-built console from stage 1 into the package tree so
# FastAPI serves it from gencall/web/console (see gencall/main.py CONSOLE_DIR).
COPY --from=frontend /build/gencall/web/console ./gencall/web/console

# Install the package itself.
RUN pip install --no-cache-dir -e .

# ── Runtime directories ──────────────────────────────────────────────────────
# Make state dirs writable by the non-root UBI user (UID 1001 / group root).
RUN mkdir -p /opt/gencall/logs \
             /opt/gencall/media \
             /opt/gencall/data \
             /opt/gencall/scenarios/custom \
    && chgrp -R 0 /opt/gencall \
    && chmod -R g=u /opt/gencall

ENV GENCALL_CONFIG=/opt/gencall/gencall/etc/gencall.cfg \
    PYTHONUNBUFFERED=1

# ── Ports ────────────────────────────────────────────────────────────────────
EXPOSE 8080
EXPOSE 5060/udp
EXPOSE 5060/tcp
EXPOSE 10000-20000/udp

# ── Health check ─────────────────────────────────────────────────────────────
# Unauthenticated /api/health endpoint (see controller/worker API contract).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8080/api/health', timeout=4).status==200 else sys.exit(1)" || exit 1

# Drop privileges for runtime (matches the UBI base default non-root user).
USER 1001

# Default = worker mode. Controller mode is selected in docker-compose.yml by
# overriding the command (see the `controller` service).
ENTRYPOINT ["gencall-server"]
CMD ["--host", "0.0.0.0", "--port", "8080"]

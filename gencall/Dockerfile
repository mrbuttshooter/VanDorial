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

# ── SIPp from source ─────────────────────────────────────────────────────────
# SIPp is the SIP load-test engine and the product cannot place a call without
# it (design §1, §7 stage 1). It is NOT in the UBI9 repos and EPEL ships NO
# 'sipp' package for UBI9, so we BUILD IT FROM SOURCE and install the single
# binary to /usr/local/bin/sipp (the default in gencall.cfg [sipp] command).
#
# Built with USE_SCTP=0: SCTP support needs lksctp-tools-devel, which lives in
# the CRB/devel repo that is NOT reachable on an unsubscribed UBI host — and the
# loop runner only uses UDP/TCP/TLS (design §3 non-goals), so SCTP is dead weight
# that would only break the build. We keep PCAP (G.711 pcap play for media,
# §4.1) and SSL (TLS transport) on.
#
# Pinned to a release tag for reproducibility. The toolchain (gcc/cmake/git and
# the -devel headers) is installed, used, and then REMOVED in the same layer so
# none of it survives into the final image — only the runtime shared libs
# (libpcap, openssl) and the sipp binary remain. This keeps the image lean.
ARG SIPP_VERSION=v3.7.7
RUN set -eux; \
    dnf install -y --nodocs --setopt=install_weak_deps=False \
        libpcap-devel openssl-devel ncurses-devel \
        gcc gcc-c++ make cmake git ; \
    git clone --depth 1 -b "${SIPP_VERSION}" https://github.com/SIPp/sipp.git /tmp/sipp ; \
    cmake -S /tmp/sipp -B /tmp/sipp/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DUSE_PCAP=1 -DUSE_SSL=1 -DUSE_SCTP=0 ; \
    cmake --build /tmp/sipp/build -j "$(nproc)" ; \
    install -m 0755 /tmp/sipp/build/sipp /usr/local/bin/sipp ; \
    /usr/local/bin/sipp -v ; \
    rm -rf /tmp/sipp ; \
    dnf remove -y --noautoremove \
        libpcap-devel openssl-devel ncurses-devel \
        gcc gcc-c++ make cmake git ; \
    dnf install -y --nodocs libpcap openssl-libs ncurses-libs ; \
    dnf clean all ; \
    rm -rf /var/cache/dnf

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

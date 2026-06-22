#!/usr/bin/env bash
#
# GenCall v2 "Loop Runner" — NATIVE Ubuntu installer (NO Docker).
#
# Installs straight onto the host with systemd, apt, PostgreSQL and SIPp — the same
# way you run sigma. Run as root from inside the cloned repo:
#
#     sudo ./deploy/install-ubuntu.sh
#
# It is idempotent (safe to re-run). Override prompts with env vars, e.g.:
#     PG_PASSWORD=xxx RTP_RANGE=16384-16584 sudo -E ./deploy/install-ubuntu.sh
#
# Target: Ubuntu 22.04 / 24.04, 4 vCPU / 4 GB. Needs UDP/5060 free (do NOT co-locate
# with sigma on the same box unless you change GenCall's SIP port).
#
set -euo pipefail

INSTALL_DIR=/opt/gencall
SIPP_VERSION="${SIPP_VERSION:-v3.7.3}"
GC_USER=gencall
RTP_RANGE="${RTP_RANGE:-16384-16584}"
PORT="${PORT:-8080}"          # web/API port — override e.g. PORT=8000

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo $0"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$REPO/setup.py" ] && [ -d "$REPO/gencall" ] || die "Run from inside the cloned repo (setup.py not found)."
. /etc/os-release 2>/dev/null || true
[ "${ID:-}" = "ubuntu" ] || warn "This script targets Ubuntu; ${PRETTY_NAME:-this OS} may need tweaks."

RTP_LO="${RTP_RANGE%-*}"; RTP_HI="${RTP_RANGE#*-}"

# ── 0. Role: worker (headless) or controller (full console / web app) ─────────
# A worker runs the REST API + loop engine ONLY (no dashboard) and is driven from
# a controller. A controller serves the full console / web app on :8080. Override
# non-interactively with ROLE=worker|controller.
ROLE="${ROLE:-}"
if [ -z "$ROLE" ]; then
  printf '\n   How will this box be used?\n'
  printf '     [w] worker     — headless: REST API + loop engine, NO dashboard / web app\n'
  printf '     [c] controller — full console + dashboard / web app to drive the fleet\n'
  read -rp "   Role [w/c] (default: w): " _role || true
  case "${_role,,}" in c|controller) ROLE=controller;; *) ROLE=worker;; esac
fi
case "$ROLE" in
  worker)     SERVE_CONSOLE=false; HEADLESS_FLAG=" --headless" ;;
  controller) SERVE_CONSOLE=true;  HEADLESS_FLAG="" ;;
  *) die "ROLE must be 'worker' or 'controller' (got: $ROLE)" ;;
esac
ok "role: $ROLE — dashboard/web app $([ "$SERVE_CONSOLE" = true ] && echo ENABLED || echo DISABLED)"

# ── 0b. Transport: HTTP or HTTPS (self-signed, native uvicorn TLS) ────────────
# The app serves TLS natively (uvicorn) when [web] ssl=true + ssl_cert/ssl_key are
# set; we generate a self-signed cert below. Override non-interactively with
# GENCALL_TLS=http|https. Default: http (preserves prior behavior).
GENCALL_TLS="${GENCALL_TLS:-}"
if [ -z "$GENCALL_TLS" ]; then
  if [ -t 0 ]; then
    printf '\n   Serve the web/API over HTTP or HTTPS?\n'
    printf '     [p] http  — plain HTTP (default)\n'
    printf '     [s] https — self-signed TLS cert generated for this box\n'
    read -rp "   Transport [p/s] (default: p): " _tls || true
    case "${_tls,,}" in s|https) GENCALL_TLS=https;; *) GENCALL_TLS=http;; esac
  else
    GENCALL_TLS=http
  fi
fi
case "$GENCALL_TLS" in
  http)  USE_TLS=false; SCHEME=http  ;;
  https) USE_TLS=true;  SCHEME=https ;;
  *) die "GENCALL_TLS must be 'http' or 'https' (got: $GENCALL_TLS)" ;;
esac
ok "transport: $SCHEME"

# ── 1. System packages ────────────────────────────────────────────────────────
say "Installing system packages (python, postgresql, SIPp build deps)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-pip \
  postgresql postgresql-contrib \
  build-essential cmake git \
  libpcap-dev libssl-dev libncurses-dev \
  curl tcpdump >/dev/null
ok "base packages installed"

# ── 2. SIPp from source → /usr/local/bin/sipp (matches gencall.cfg default) ────
# Built with PCAP + SSL, no SCTP (loop runner only needs UDP/TCP/TLS). Pinned tag.
if command -v sipp >/dev/null 2>&1 && sipp -v >/dev/null 2>&1; then
  ok "SIPp already present: $(sipp -v 2>/dev/null | head -1)"
else
  say "Building SIPp ${SIPP_VERSION} from source (a few minutes)"
  rm -rf /tmp/sipp
  git clone --depth 1 -b "$SIPP_VERSION" https://github.com/SIPp/sipp.git /tmp/sipp >/dev/null 2>&1
  cmake -S /tmp/sipp -B /tmp/sipp/build -DCMAKE_BUILD_TYPE=Release \
        -DUSE_PCAP=1 -DUSE_SSL=1 -DUSE_SCTP=0 >/dev/null
  cmake --build /tmp/sipp/build -j "$(nproc)" >/dev/null
  install -m 0755 /tmp/sipp/build/sipp /usr/local/bin/sipp
  rm -rf /tmp/sipp
  ok "SIPp installed: $(/usr/local/bin/sipp -v 2>/dev/null | head -1)"
fi
# RTP media (play_pcap_audio) sends via a RAW socket → needs CAP_NET_RAW, else
# SIPp segfaults when an RTP loop runs as the non-root service user. Grant it.
SIPP_PATH="$(command -v sipp || echo /usr/local/bin/sipp)"
if command -v setcap >/dev/null 2>&1; then
  setcap cap_net_raw+ep "$SIPP_PATH" 2>/dev/null \
    && ok "granted cap_net_raw to $SIPP_PATH (RTP media)" \
    || warn "could not setcap $SIPP_PATH — RTP-media loops need: sudo setcap cap_net_raw+ep $SIPP_PATH"
fi

# tcpdump for on-demand pcap capture (Trace). It is installed with the base
# packages above; grant it capture caps so the non-root gencall service user can
# run it without sudo.
command -v tcpdump >/dev/null 2>&1 || apt-get install -y -qq tcpdump >/dev/null || true
if command -v setcap >/dev/null 2>&1 && command -v tcpdump >/dev/null 2>&1; then
  setcap cap_net_raw,cap_net_admin+eip "$(command -v tcpdump)" 2>/dev/null \
    && ok "granted capture caps to tcpdump (Trace)" \
    || warn "could not setcap tcpdump — captures need: sudo setcap cap_net_raw,cap_net_admin+eip \$(command -v tcpdump)"
fi

# ── 3. Service user ───────────────────────────────────────────────────────────
id "$GC_USER" >/dev/null 2>&1 || { useradd --system --create-home --shell /usr/sbin/nologin "$GC_USER"; ok "created system user '$GC_USER'"; }

# ── 4. PostgreSQL: role + database ────────────────────────────────────────────
say "Configuring PostgreSQL"
systemctl enable --now postgresql >/dev/null 2>&1 || true
PG_PASSWORD="${PG_PASSWORD:-}"
if [ -z "$PG_PASSWORD" ]; then
  if [ -f "$INSTALL_DIR/.dbpass" ]; then
    PG_PASSWORD="$(cat "$INSTALL_DIR/.dbpass")"; ok "reusing existing DB password"
  else
    PG_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"; ok "generated a DB password"
  fi
fi
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='gencall'" | grep -q 1 \
  && sudo -u postgres psql -qc "ALTER USER gencall WITH PASSWORD '${PG_PASSWORD}';" \
  || sudo -u postgres psql -qc "CREATE USER gencall WITH PASSWORD '${PG_PASSWORD}';"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='gencall'" | grep -q 1 \
  || sudo -u postgres psql -qc "CREATE DATABASE gencall OWNER gencall;"
ok "PostgreSQL role + database 'gencall' ready"

# ── 5. Install the app into /opt/gencall (venv) ───────────────────────────────
say "Installing GenCall into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# Copy the repo (skip VCS / node / caches / local secrets); keep the prebuilt
# console at gencall/web/console which the worker serves (no Node needed).
tar -C "$REPO" --exclude='.git' --exclude='node_modules' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='.env' --exclude='gencall-v2.zip' -cf - . \
  | tar -C "$INSTALL_DIR" -xf -
[ -d "$INSTALL_DIR/gencall/web/console" ] && ok "console bundle present (served at /console)" \
  || warn "prebuilt console not found — the API still works; rebuild frontend if you want the UI"
mkdir -p "$INSTALL_DIR/logs" "$INSTALL_DIR/data" "$INSTALL_DIR/gencall/media"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
"$INSTALL_DIR/venv/bin/pip" install -q -e "$INSTALL_DIR"
printf '%s' "$PG_PASSWORD" > "$INSTALL_DIR/.dbpass"; chmod 600 "$INSTALL_DIR/.dbpass"
ok "venv built + package installed"

# ── 6. Configure gencall.cfg (RTP window, SIPp path, MADA whitelist) ──────────
say "Writing configuration"
CFG="$INSTALL_DIR/gencall/etc/gencall.cfg"
set_cfg() { # section key value
  python3 - "$CFG" "$1" "$2" "$3" <<'PY'
import configparser, sys
p, s, k, v = sys.argv[1:5]
c = configparser.ConfigParser()
c.read(p)
if not c.has_section(s):
    c.add_section(s)
c.set(s, k, v)
with open(p, "w") as f:
    c.write(f)
PY
}
set_cfg sipp command /usr/local/bin/sipp
set_cfg sip min_rtp_port "$RTP_LO"
set_cfg sip max_rtp_port "$RTP_HI"
# Headless worker => no console/web app + no live-stats WebSocket; controller => full console.
set_cfg web serve_console "$SERVE_CONSOLE"

# HTTPS: generate a self-signed cert (idempotent) and point [web] ssl at it.
if [ "$USE_TLS" = true ]; then
  CERT_DIR="$INSTALL_DIR/certs"
  CERT="$CERT_DIR/gencall.crt"
  KEY="$CERT_DIR/gencall.key"
  mkdir -p "$CERT_DIR"
  # Service user must own/traverse this dir to read the key (else uvicorn dies
  # with PermissionError and systemd crash-loops the worker).
  chown "$GC_USER":"$GC_USER" "$CERT_DIR" 2>/dev/null || true
  chmod 750 "$CERT_DIR"
  if [ -f "$CERT" ] && [ -f "$KEY" ]; then
    ok "TLS cert already present ($CERT) — reusing"
  else
    # CN/SAN = the box's primary IP (falls back to hostname) so clients that pin
    # the host match the cert. 2048-bit RSA, ~10 years.
    HOST_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -n "$HOST_ADDR" ] || HOST_ADDR="$(hostname -f 2>/dev/null || hostname)"
    if printf '%s' "$HOST_ADDR" | grep -qE '^[0-9]+(\.[0-9]+){3}$'; then
      SAN="IP:$HOST_ADDR"
    else
      SAN="DNS:$HOST_ADDR"
    fi
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -keyout "$KEY" -out "$CERT" \
      -subj "/CN=$HOST_ADDR" -addext "subjectAltName=$SAN" >/dev/null 2>&1 \
      || die "openssl failed to generate the self-signed cert"
    ok "generated self-signed TLS cert (CN=$HOST_ADDR, $SAN, 3650d)"
  fi
  # Tight perms — the key is owned by the service user that runs gencall.
  chmod 600 "$KEY"; chmod 644 "$CERT"
  chown "$GC_USER":"$GC_USER" "$KEY" "$CERT"
  set_cfg web ssl true
  set_cfg web ssl_cert "$CERT"
  set_cfg web ssl_key "$KEY"
  ok "TLS enabled in config ([web] ssl=true, cert=$CERT)"
fi
# Inbound trust whitelist is no longer set here — configure it from the
# controller console (Configuration → Inbound Trust), which pushes it to every
# worker at runtime. The HOST FIREWALL remains the real boundary (see docs).
ok "config written ($CFG); RTP $RTP_LO-$RTP_HI, sipp=/usr/local/bin/sipp"

# DB URL the worker uses — also used to mint the API key right now.
DB_URL="postgresql://gencall:${PG_PASSWORD}@127.0.0.1:5432/gencall"

# ── 6b. Mint the admin API key NOW ────────────────────────────────────────────
# Done at install time (not left to first-boot) so it's printed for you and the
# service won't auto-mint a different one. Goes into the SAME DB the worker reads.
say "Minting API key"
API_KEY="$(GENCALL_CONFIG="$CFG" GENCALL_DB_ENGINE=postgresql GENCALL_DATABASE_URL="$DB_URL" \
  "$INSTALL_DIR/venv/bin/gencall" keys create --name "${ROLE}-admin" 2>/dev/null \
  | sed -n 's/.*X-API-Key:[[:space:]]*//p' | tail -1)"
if [ -n "$API_KEY" ]; then
  printf '%s\n' "$API_KEY" > "$INSTALL_DIR/.api_key"; chmod 600 "$INSTALL_DIR/.api_key"
  ok "API key minted (saved to $INSTALL_DIR/.api_key)"
else
  warn "could not mint a key now — the worker mints one on first boot: journalctl -u gencall-worker | grep X-API-Key"
fi

# ── 6c. Initial console login account ─────────────────────────────────────────
# The console now requires a login. Create one admin account at install time
# (idempotent — only if no console users exist yet). Username: GENCALL_ADMIN_USER
# (default admin). Password: GENCALL_ADMIN_PASSWORD, else a strong random one is
# generated and printed once. The password is passed via GENCALL_USER_PASSWORD
# env (NOT argv) so it never lands in ps/shell history. Min length 8.
say "Creating initial console account"
ADMIN_USER="${GENCALL_ADMIN_USER:-admin}"
GC_ENV=(GENCALL_CONFIG="$CFG" GENCALL_DB_ENGINE=postgresql GENCALL_DATABASE_URL="$DB_URL")
# Mirror the keys check: ask the CLI what already exists. `users list` prints a
# table header ("username ... role ...") only when users exist; an empty install
# prints "No console users." (which also contains the word "username"), so match
# the header columns, not the bare word.
if env "${GC_ENV[@]}" "$INSTALL_DIR/venv/bin/gencall" users list 2>/dev/null | grep -qE 'username[[:space:]]+role'; then
  ok "console account(s) already exist — leaving them untouched"
  ADMIN_PASSWORD=""
else
  ADMIN_PASSWORD="${GENCALL_ADMIN_PASSWORD:-}"
  if [ -z "$ADMIN_PASSWORD" ]; then
    ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)"
    GEN_PW=1
  else
    GEN_PW=0
  fi
  if env "${GC_ENV[@]}" GENCALL_USER_PASSWORD="$ADMIN_PASSWORD" \
       "$INSTALL_DIR/venv/bin/gencall" users create "$ADMIN_USER" >/dev/null 2>&1; then
    ok "console account '$ADMIN_USER' created"
  else
    warn "could not create the console account now — create one later: ${INSTALL_DIR}/venv/bin/gencall users create $ADMIN_USER"
    ADMIN_PASSWORD=""
  fi
fi

chown -R "$GC_USER":"$GC_USER" "$INSTALL_DIR"

# ── 7. systemd services ───────────────────────────────────────────────────────
say "Installing systemd services"

cat > /etc/systemd/system/gencall-worker.service <<EOF
[Unit]
Description=GenCall v2 worker (SIP traffic generator + loop engine)
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=simple
User=${GC_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=GENCALL_CONFIG=${CFG}
Environment=GENCALL_DB_ENGINE=postgresql
Environment=GENCALL_DATABASE_URL=${DB_URL}
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/venv/bin/gencall-server --host 0.0.0.0 --port ${PORT}${HEADLESS_FLAG}
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

# Controller (multi-box fleet console). Installed but OPTIONAL for a single box —
# the worker already serves the console + Loops page on :8080.
cat > /etc/systemd/system/gencall-controller.service <<EOF
[Unit]
Description=GenCall v2 controller (fleet control plane + console) — optional, multi-box
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${GC_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=GENCALL_CONFIG=${CFG}
Environment=GENCALL_DATABASE_URL=sqlite:////${INSTALL_DIR#/}/data/controller.db
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/venv/bin/gencall-server --mode controller --host 0.0.0.0 --port 8090
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now gencall-worker >/dev/null 2>&1
ok "gencall-worker enabled + started (DB migrations apply automatically at boot)"
warn "gencall-controller installed but NOT started (only needed for multi-box; enable with: systemctl enable --now gencall-controller)"

# ── 8. Health check + next steps ──────────────────────────────────────────────
say "Health check"
sleep 4
if curl -fskS "${SCHEME}://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  ok "worker /api/health OK"
else
  warn "worker not healthy yet — check:  journalctl -u gencall-worker -n 50 --no-pager"
fi

say "Done"
echo
echo "   +-- API KEY (send as the  X-API-Key:  header) --------------------------"
if [ -n "${API_KEY:-}" ]; then
  echo "   |  ${API_KEY}"
  echo "   |  (also saved to ${INSTALL_DIR}/.api_key)"
else
  echo "   |  mint one now:  ${INSTALL_DIR}/venv/bin/gencall keys create --name admin"
fi
echo "   +----------------------------------------------------------------------"
echo
if [ -n "${ADMIN_PASSWORD:-}" ]; then
  echo "   +-- CONSOLE LOGIN (shown once) -----------------------------------------"
  echo "   |  username:  ${ADMIN_USER}"
  echo "   |  password:  ${ADMIN_PASSWORD}"
  [ "${GEN_PW:-0}" = 1 ] && echo "   |  (auto-generated — save it now; it is NOT stored anywhere)"
  echo "   +----------------------------------------------------------------------"
  echo
fi
if [ "$ROLE" = controller ]; then
  echo "   Role: CONTROLLER — open the dashboard / web app at:"
  echo "       ${SCHEME}://<box-ip>:${PORT}/console/"
  echo "   Add your worker boxes on the Nodes page (their URL + their API key)."
else
  echo "   Role: WORKER (headless — no dashboard / web app on this box)."
  echo "   Register it on the CONTROLLER: Nodes page -> Add node -> Runs on ="
  echo "       ${SCHEME}://<this-box-ip>:${PORT}   + the API key above."
fi
echo
echo "   FIREWALL (the REAL trust boundary): restrict UDP/5060 + ${RTP_LO}-${RTP_HI} to"
echo "   the MADA whitelist (${MADA:-<set this>}).  Rules: docs/deploy/loop-runner.md section 2"
echo
echo "   Manage keys:  ${INSTALL_DIR}/venv/bin/gencall keys list"
echo "   Logs:         journalctl -u gencall-worker -f"
echo "   Restart:      systemctl restart gencall-worker"
ok "install-ubuntu.sh finished"

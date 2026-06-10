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
#     PG_PASSWORD=xxx RTP_RANGE=16384-16584 MADA_IPS="203.0.113.10" sudo -E ./deploy/install-ubuntu.sh
#
# Target: Ubuntu 22.04 / 24.04, 4 vCPU / 4 GB. Needs UDP/5060 free (do NOT co-locate
# with sigma on the same box unless you change GenCall's SIP port).
#
set -euo pipefail

INSTALL_DIR=/opt/gencall
SIPP_VERSION="${SIPP_VERSION:-v3.7.3}"
GC_USER=gencall
RTP_RANGE="${RTP_RANGE:-16384-16584}"

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
MADA="${MADA_IPS:-}"
if [ -z "$MADA" ]; then
  read -rp "   MADA signalling IP(s) for the inbound whitelist [blank = set later]: " MADA || true
fi
[ -n "$MADA" ] && { set_cfg trust whitelist "$MADA"; ok "[trust] whitelist = $MADA"; } \
               || warn "trust whitelist empty — inbound calls will be FLAGGED until you set it"
ok "config written ($CFG); RTP $RTP_LO-$RTP_HI, sipp=/usr/local/bin/sipp"

chown -R "$GC_USER":"$GC_USER" "$INSTALL_DIR"

# ── 7. systemd services ───────────────────────────────────────────────────────
say "Installing systemd services"
DB_URL="postgresql://gencall:${PG_PASSWORD}@127.0.0.1:5432/gencall"

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
ExecStart=${INSTALL_DIR}/venv/bin/gencall-server --host 0.0.0.0 --port 8080
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
if curl -fsS http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
  ok "worker /api/health OK"
else
  warn "worker not healthy yet — check:  journalctl -u gencall-worker -n 50 --no-pager"
fi

say "Done — two things left for YOU"
cat <<EOF
   1) API KEY: the worker minted an admin key on first boot. Grab it:
        journalctl -u gencall-worker | grep -A1 'X-API-Key' | tail -2
      Save it (shown once). Manage keys:  ${INSTALL_DIR}/venv/bin/gencall keys list

   2) FIREWALL (the REAL trust boundary): restrict UDP/5060 + ${RTP_LO}-${RTP_HI} to
      the MADA whitelist (${MADA:-<set this>}). nftables/ufw rules are in:
        docs/deploy/loop-runner.md  (section 2)

   Console + Loops page:   http://<box-ip>:8080/console/
   Logs:                   journalctl -u gencall-worker -f
   Restart / stop:         systemctl restart|stop gencall-worker
   SIPp sanity:            sipp -v
EOF
ok "install-ubuntu.sh finished"

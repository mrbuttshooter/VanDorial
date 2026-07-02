#!/usr/bin/env bash
#
# GenCall v2 "Loop Runner" — OFFLINE / AIR-GAPPED Ubuntu installer.
#
# NO internet, NO apt, NO SIPp build, NO PostgreSQL. Fully self-contained — it ships:
#   - SIPp                   (vendor/debs/ — sip-tester + libs, dpkg-installed if no sipp)
#   - the venv builder       (vendor/virtualenv.pyz — so NO OS python3-venv needed)
#   - all Python libs        (vendor/wheelhouse/ — installed offline with --no-index)
#   - SQLite                 (built into Python — no DB server to install)
#
# Run as root from inside the unzipped offline bundle:
#     sudo ./deploy/install-offline.sh
#
# Idempotent. The ONLY box prerequisite is python3 >= 3.10 (Ubuntu ships it by
# default). Needs UDP/5060 free. The bundled SIPp/venv/lib artifacts are built for
# Ubuntu 22.04 / Python 3.10 (cy213/cy214) — for a different OS/Python, refresh them
# with deploy/build-debs.sh + deploy/build-wheelhouse.sh on a matching online box.
#
set -euo pipefail

INSTALL_DIR=/opt/gencall
GC_USER=gencall
RTP_RANGE="${RTP_RANGE:-16384-16584}"
PORT="${PORT:-8080}"          # web/API port — override e.g. PORT=8000

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo $0"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$REPO/pyproject.toml" ] && [ -d "$REPO/gencall" ] || die "Run from inside the unzipped bundle (pyproject.toml not found)."
WHEELHOUSE="$REPO/vendor/wheelhouse"
[ -d "$WHEELHOUSE" ] && ls "$WHEELHOUSE"/*.whl >/dev/null 2>&1 || die "Wheelhouse missing: $WHEELHOUSE — use the release bundle (it ships the wheels), or build one on an online box of the same Python: deploy/build-wheelhouse.sh."
RTP_LO="${RTP_RANGE%-*}"; RTP_HI="${RTP_RANGE#*-}"

# ── 0. Role: worker (headless) or controller (full console / web app) ─────────
# Worker = REST API + loop engine ONLY (no dashboard), driven from a controller.
# Controller = full console / web app on :8080. Override with ROLE=worker|controller.
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
# set; we generate a self-signed cert below with openssl (ships in the Ubuntu base
# image — air-gapped friendly). Override with GENCALL_TLS=http|https. Default: http.
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
[ "$USE_TLS" = true ] && command -v openssl >/dev/null 2>&1 \
  || [ "$USE_TLS" = false ] \
  || die "GENCALL_TLS=https but 'openssl' is not on PATH (air-gapped — add it to the base image)."
ok "transport: $SCHEME"

# ── 1. Preconditions + bundled OS packages ────────────────────────────────────
say "Checking prerequisites (offline — nothing is downloaded)"

# SIPp: if the box has no 'sipp' and the bundle carries vendor/debs/*.deb, install
# them (sip-tester + libgsl/libsctp/libpcap). Two dpkg passes resolve inter-deps;
# a failure is tolerated here and surfaces in the sipp check just below.
DEBS="$REPO/vendor/debs"
if ! command -v sipp >/dev/null 2>&1 && ls "$DEBS"/*.deb >/dev/null 2>&1; then
  say "Installing bundled SIPp from vendor/debs (offline)"
  dpkg -i "$DEBS"/*.deb >/dev/null 2>&1 || dpkg -i "$DEBS"/*.deb || warn "some bundled .debs failed (a base lib may be missing) — see the sipp check below"
  ok "vendor/debs installed ($(ls "$DEBS"/*.deb | wc -l) packages)"
fi

SIPP_BIN="${SIPP_BIN:-$(command -v sipp || true)}"
[ -n "$SIPP_BIN" ] || die "No 'sipp' on PATH. The bundle ships sip-tester in vendor/debs (auto-installed) — if that failed, install sip-tester, copy a sipp binary (SIPP_BIN=/path/to/sipp), or run deploy/build-debs.sh on a matching online box."
ok "SIPp: $SIPP_BIN ($("$SIPP_BIN" -v 2>/dev/null | head -1))"
command -v python3 >/dev/null || die "python3 missing (Ubuntu ships python3 by default)"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)' || die "Need Python >= 3.10 (have $(python3 -V))"
# venv: the bundle ships virtualenv.pyz (a self-contained zipapp), so the OS
# python3-venv package is NOT required. Only fall back to needing it if the pyz is
# absent (e.g. a stripped bundle).
VENV_PYZ="$REPO/vendor/virtualenv.pyz"
if [ ! -f "$VENV_PYZ" ]; then
  python3 -c 'import venv, ensurepip' 2>/dev/null || die "vendor/virtualenv.pyz absent AND python3-venv missing — add the pyz or 'apt install python3-venv'."
fi
ok "Python: $(python3 -V 2>&1)"
# The wheelhouse's native wheels (uvloop, pydantic_core, psycopg2, …) are locked to
# a Python ABI (cp3X). A box on a different Python would fail later with a cryptic
# pip error — catch it here with a clear fix.
PYTAG="cp$(python3 -c 'import sys;print(f"{sys.version_info[0]}{sys.version_info[1]}")')"
if ls "$WHEELHOUSE"/*-cp3*-*.whl >/dev/null 2>&1 && ! ls "$WHEELHOUSE"/*-"${PYTAG}"-*.whl >/dev/null 2>&1; then
  HAVE="$(ls "$WHEELHOUSE"/*-cp3*-*.whl 2>/dev/null | sed -E 's/.*-(cp3[0-9]+)-.*/\1/' | sort -u | tr '\n' ' ')"
  die "Wheelhouse built for [${HAVE}] but this box is ${PYTAG}. Rebuild it on a ${PYTAG} box: deploy/build-wheelhouse.sh (or install a matching Python)."
fi
ok "wheelhouse matches this box ($PYTAG)"

# ── 2. Service user ───────────────────────────────────────────────────────────
id "$GC_USER" >/dev/null 2>&1 || { useradd --system --create-home --shell /usr/sbin/nologin "$GC_USER"; ok "created system user '$GC_USER'"; }

# ── 3. Install the app into /opt/gencall ──────────────────────────────────────
say "Installing GenCall into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
tar -C "$REPO" --exclude='.git' --exclude='node_modules' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='.env' -cf - . | tar -C "$INSTALL_DIR" -xf -
mkdir -p "$INSTALL_DIR/logs" "$INSTALL_DIR/data" "$INSTALL_DIR/gencall/media"
[ -d "$INSTALL_DIR/gencall/web/console" ] && ok "console bundle present (served at /console)" \
  || warn "prebuilt console not found — API still works"

# ── 4. venv + OFFLINE pip from the wheelhouse ─────────────────────────────────
say "Building venv and installing Python libs from the wheelhouse (no network)"
if [ -f "$VENV_PYZ" ]; then
  # Self-contained zipapp: bundles pip/setuptools seed wheels, so the venv builds
  # with NO OS python3-venv / python3-pip package on the box.
  python3 "$VENV_PYZ" --no-periodic-update "$INSTALL_DIR/venv"
else
  python3 -m venv "$INSTALL_DIR/venv"
fi
PIP="$INSTALL_DIR/venv/bin/pip"
# The bundle now ships pip/setuptools/wheel in the wheelhouse, so the PEP 517
# build below does not depend on whatever the venv seed happened to provide.
# setuptools (>=64, PEP 660 editable) + wheel are REQUIRED to build gencall from
# pyproject.toml with --no-build-isolation, so install them strictly (fail loudly
# with a clear message rather than at the cryptic build step). pip itself is a
# best-effort upgrade.
"$PIP" install --no-index --find-links="$WHEELHOUSE" --upgrade pip >/dev/null 2>&1 || true
"$PIP" install --no-index --find-links="$WHEELHOUSE" --upgrade setuptools wheel \
  || die "Could not install setuptools/wheel from the wheelhouse — the bundle is missing the build backend. Rebuild it on a matching-Python online box: deploy/build-wheelhouse.sh"
"$PIP" install --no-index --find-links="$WHEELHOUSE" -r "$INSTALL_DIR/requirements.txt"
# Install the package itself (editable; --no-build-isolation so pip uses the
# venv's setuptools instead of trying to fetch build deps from the internet).
"$PIP" install --no-index --no-build-isolation -e "$INSTALL_DIR"
ok "Python libs + gencall installed offline"

# ── 5. Configuration (system sipp, RTP window) ───────────────────────────────
say "Writing configuration"
CFG="$INSTALL_DIR/gencall/etc/gencall.cfg"
set_cfg() { # section key value
  python3 - "$CFG" "$1" "$2" "$3" <<'PY'
import configparser, sys
p, s, k, v = sys.argv[1:5]
c = configparser.ConfigParser(); c.read(p)
if not c.has_section(s): c.add_section(s)
c.set(s, k, v)
with open(p, "w") as f: c.write(f)
PY
}
set_cfg sipp command "$SIPP_BIN"
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
  chmod 600 "$KEY"; chmod 644 "$CERT"
  chown "$GC_USER":"$GC_USER" "$KEY" "$CERT"
  set_cfg web ssl true
  set_cfg web ssl_cert "$CERT"
  set_cfg web ssl_key "$KEY"
  ok "TLS enabled in config ([web] ssl=true, cert=$CERT)"
fi
# RTP media (play_pcap_audio) sends via a RAW socket → needs CAP_NET_RAW, else
# SIPp segfaults when a loop has RTP enabled while running as the non-root
# service user. Grant it to the binary (no-op for signaling-only loops).
if command -v setcap >/dev/null 2>&1; then
  setcap cap_net_raw+ep "$SIPP_BIN" 2>/dev/null \
    && ok "granted cap_net_raw to $SIPP_BIN (RTP media)" \
    || warn "could not setcap $SIPP_BIN — RTP-media loops need: sudo setcap cap_net_raw+ep $SIPP_BIN"
fi
# tcpdump for on-demand pcap capture (Trace). This installer is AIR-GAPPED, so we
# do NOT fetch tcpdump from the network — it should already be on the base image.
# Grant it capture caps so the non-root gencall user can run it without sudo;
# warn (not fail) if it is absent so a signaling-only deploy still completes.
if command -v tcpdump >/dev/null 2>&1; then
  if command -v setcap >/dev/null 2>&1; then
    setcap cap_net_raw,cap_net_admin+eip "$(command -v tcpdump)" 2>/dev/null \
      && ok "granted capture caps to tcpdump (Trace)" \
      || warn "could not setcap tcpdump — captures need: sudo setcap cap_net_raw,cap_net_admin+eip \$(command -v tcpdump)"
  fi
else
  warn "tcpdump not found — on-demand Trace capture is unavailable until you install it (air-gapped: add it to the base image / bundle)."
fi
# Inbound trust whitelist is no longer set here — configure it from the
# controller console (Configuration → Inbound Trust), which pushes it to every
# worker at runtime. The HOST FIREWALL remains the real boundary (see docs).
ok "config: sipp=$SIPP_BIN, RTP $RTP_LO-$RTP_HI, DB=SQLite"

# DB URL the worker uses (SQLite) — also used to mint the API key right now.
DB_URL="sqlite:////${INSTALL_DIR#/}/data/gencall.db"

# ── 5b. Mint the admin API key NOW ────────────────────────────────────────────
# So it's printed for you at install time and the service won't auto-mint another.
say "Minting API key"
API_KEY="$(GENCALL_CONFIG="$CFG" GENCALL_DB_ENGINE=sqlite GENCALL_DATABASE_URL="$DB_URL" \
  "$INSTALL_DIR/venv/bin/gencall" keys create --name "${ROLE}-admin" 2>/dev/null \
  | sed -n 's/.*X-API-Key:[[:space:]]*//p' | tail -1)"
if [ -n "$API_KEY" ]; then
  printf '%s\n' "$API_KEY" > "$INSTALL_DIR/.api_key"; chmod 600 "$INSTALL_DIR/.api_key"
  ok "API key minted (saved to $INSTALL_DIR/.api_key)"
else
  warn "could not mint a key now — the worker mints one on first boot: journalctl -u gencall-worker | grep X-API-Key"
fi

# ── 5c. Initial console login account ─────────────────────────────────────────
# The console now requires a login. Create one admin account at install time
# (idempotent — only if no console users exist yet). Username: GENCALL_ADMIN_USER
# (default admin). Password: GENCALL_ADMIN_PASSWORD, else a strong random one is
# generated and printed once. Passed via GENCALL_USER_PASSWORD env (NOT argv) so it
# never lands in ps/shell history. Min length 8. openssl is required for the random
# fallback — guard so a signaling-only / no-openssl box still completes.
say "Creating initial console account"
ADMIN_USER="${GENCALL_ADMIN_USER:-admin}"
GC_ENV=(GENCALL_CONFIG="$CFG" GENCALL_DB_ENGINE=sqlite GENCALL_DATABASE_URL="$DB_URL")
if env "${GC_ENV[@]}" "$INSTALL_DIR/venv/bin/gencall" users list 2>/dev/null | grep -qE 'username[[:space:]]+role'; then
  ok "console account(s) already exist — leaving them untouched"
  ADMIN_PASSWORD=""
else
  ADMIN_PASSWORD="${GENCALL_ADMIN_PASSWORD:-}"
  GEN_PW=0
  if [ -z "$ADMIN_PASSWORD" ]; then
    if command -v openssl >/dev/null 2>&1; then
      ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)"; GEN_PW=1
    else
      warn "no GENCALL_ADMIN_PASSWORD and openssl absent — create a console user later: ${INSTALL_DIR}/venv/bin/gencall users create $ADMIN_USER"
    fi
  fi
  if [ -n "$ADMIN_PASSWORD" ] && env "${GC_ENV[@]}" GENCALL_USER_PASSWORD="$ADMIN_PASSWORD" \
       "$INSTALL_DIR/venv/bin/gencall" users create "$ADMIN_USER" >/dev/null 2>&1; then
    ok "console account '$ADMIN_USER' created"
  else
    [ -n "$ADMIN_PASSWORD" ] && warn "could not create the console account now — create one later: ${INSTALL_DIR}/venv/bin/gencall users create $ADMIN_USER"
    ADMIN_PASSWORD=""
  fi
fi

chown -R "$GC_USER":"$GC_USER" "$INSTALL_DIR"

# ── 6. systemd service (SQLite — no DB server) ────────────────────────────────
say "Installing systemd service"
cat > /etc/systemd/system/gencall-worker.service <<EOF
[Unit]
Description=GenCall v2 worker (SIP traffic generator + loop engine)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${GC_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=GENCALL_CONFIG=${CFG}
Environment=GENCALL_DB_ENGINE=sqlite
Environment=GENCALL_DATABASE_URL=${DB_URL}
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/venv/bin/gencall-server --host 0.0.0.0 --port ${PORT}${HEADLESS_FLAG}
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now gencall-worker >/dev/null 2>&1
ok "gencall-worker enabled + started (SQLite DB at ${INSTALL_DIR}/data/gencall.db; migrations auto-apply)"

# ── 6b. Disk-hygiene hardening (journald cap, retention cap, /tmp sweep) ───────
say "Applying disk-hygiene hardening"
GENCALL_CFG="$CFG" bash "$REPO/deploy/harden-disk.sh" || warn "disk hardening step had a problem (non-fatal)"

# ── 7. Health check + next steps ──────────────────────────────────────────────
say "Health check"
sleep 4
if command -v curl >/dev/null 2>&1 && curl -fskS "${SCHEME}://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  ok "worker /api/health OK"
elif python3 -c "import urllib.request,ssl,sys; ctx=ssl._create_unverified_context(); sys.exit(0 if urllib.request.urlopen('${SCHEME}://127.0.0.1:${PORT}/api/health',timeout=4,context=ctx).status==200 else 1)" 2>/dev/null; then
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
echo "   your MADA signalling IP(s).  Rules: docs/deploy/loop-runner.md section 2"
echo
echo "   Manage keys:  ${INSTALL_DIR}/venv/bin/gencall keys list"
echo "   Logs:         journalctl -u gencall-worker -f"
echo "   SIPp sanity:  $SIPP_BIN -v"
ok "install-offline.sh finished"

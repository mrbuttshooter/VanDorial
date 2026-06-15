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

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo $0"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$REPO/setup.py" ] && [ -d "$REPO/gencall" ] || die "Run from inside the unzipped bundle (setup.py not found)."
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
# Upgrade pip/setuptools/wheel from the wheelhouse if present (tolerant — the
# venv already ships a usable pip+setuptools, so failure here is non-fatal).
"$PIP" install --no-index --find-links="$WHEELHOUSE" --upgrade pip setuptools wheel >/dev/null 2>&1 || true
"$PIP" install --no-index --find-links="$WHEELHOUSE" -r "$INSTALL_DIR/requirements.txt"
# Install the package itself (editable; --no-build-isolation so pip uses the
# venv's setuptools instead of trying to fetch build deps from the internet).
"$PIP" install --no-index --no-build-isolation -e "$INSTALL_DIR"
ok "Python libs + gencall installed offline"

# ── 5. Configuration (system sipp, RTP window, MADA whitelist) ────────────────
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
# RTP media (play_pcap_audio) sends via a RAW socket → needs CAP_NET_RAW, else
# SIPp segfaults when a loop has RTP enabled while running as the non-root
# service user. Grant it to the binary (no-op for signaling-only loops).
if command -v setcap >/dev/null 2>&1; then
  setcap cap_net_raw+ep "$SIPP_BIN" 2>/dev/null \
    && ok "granted cap_net_raw to $SIPP_BIN (RTP media)" \
    || warn "could not setcap $SIPP_BIN — RTP-media loops need: sudo setcap cap_net_raw+ep $SIPP_BIN"
fi
MADA="${MADA_IPS:-}"
if [ -z "$MADA" ]; then
  read -rp "   MADA signalling IP(s) for the inbound whitelist [blank = set later]: " MADA || true
fi
[ -n "$MADA" ] && { set_cfg trust whitelist "$MADA"; ok "[trust] whitelist = $MADA"; } \
               || warn "trust whitelist empty — inbound calls will be FLAGGED until you set it"
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
ExecStart=${INSTALL_DIR}/venv/bin/gencall-server --host 0.0.0.0 --port 8080${HEADLESS_FLAG}
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now gencall-worker >/dev/null 2>&1
ok "gencall-worker enabled + started (SQLite DB at ${INSTALL_DIR}/data/gencall.db; migrations auto-apply)"

# ── 7. Health check + next steps ──────────────────────────────────────────────
say "Health check"
sleep 4
if command -v curl >/dev/null 2>&1 && curl -fsS http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
  ok "worker /api/health OK"
elif python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=4).status==200 else 1)" 2>/dev/null; then
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
if [ "$ROLE" = controller ]; then
  echo "   Role: CONTROLLER — open the dashboard / web app at:"
  echo "       http://<box-ip>:8080/console/"
  echo "   Add your worker boxes on the Nodes page (their URL + their API key)."
else
  echo "   Role: WORKER (headless — no dashboard / web app on this box)."
  echo "   Register it on the CONTROLLER: Nodes page -> Add node -> Runs on ="
  echo "       http://<this-box-ip>:8080   + the API key above."
fi
echo
echo "   FIREWALL (the REAL trust boundary): restrict UDP/5060 + ${RTP_LO}-${RTP_HI} to"
echo "   the MADA whitelist (${MADA:-<set this>}).  Rules: docs/deploy/loop-runner.md section 2"
echo
echo "   Manage keys:  ${INSTALL_DIR}/venv/bin/gencall keys list"
echo "   Logs:         journalctl -u gencall-worker -f"
echo "   SIPp sanity:  $SIPP_BIN -v"
ok "install-offline.sh finished"

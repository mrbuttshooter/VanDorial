#!/usr/bin/env bash
#
# GenCall v2 "Loop Runner" — guided installer for the Ubuntu deploy box.
#
# Run this ON the 4 vCPU / 4 GB Ubuntu box, from the repo root:
#     chmod +x deploy/install.sh
#     ./deploy/install.sh
#
# It does NOT touch your firewall (that could lock you out) — it prints the exact
# next step for that. It DOES: check prerequisites, set up .env + gencall.cfg,
# build the SIPp-from-source image, start the stack in the correct order, and run
# health checks. Re-running it is safe (idempotent).
#
# Override any prompt non-interactively with env vars, e.g.:
#     POSTGRES_PASSWORD=xxx RTP_RANGE=16384-16584 ./deploy/install.sh
#
set -euo pipefail

COMPOSE_FILE="docker-compose.v2.yml"
CFG="gencall/etc/gencall.cfg"
ENV_FILE=".env"

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ── 0. Location + prerequisites ───────────────────────────────────────────────
say "Checking prerequisites"
[ -f "$COMPOSE_FILE" ] || die "Run this from the repo root (no $COMPOSE_FILE here)."
command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install Docker Engine first."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing ('docker compose'). Install the compose plugin."
docker info >/dev/null 2>&1 || die "Cannot talk to the Docker daemon. Are you in the 'docker' group / is dockerd running?"
ok "Docker + Compose v2 present"
COMPOSE="docker compose -f $COMPOSE_FILE"

# ── 0b. Transport: HTTP or HTTPS (self-signed, native uvicorn TLS) ────────────
# The app serves TLS natively when [web] ssl=true + ssl_cert/ssl_key are set; we
# generate a self-signed cert under gencall/etc/certs (already bind-mounted into
# the container at /opt/gencall/gencall/etc/certs). Override with
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

# A tiny helper to set an INI key=value under a [section] in gencall.cfg, using
# python3 (present on Ubuntu). Falls back to a clear manual instruction.
set_cfg() {  # set_cfg <section> <key> <value>
  local section="$1" key="$2" value="$3"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$CFG" "$section" "$key" "$value" <<'PY'
import configparser, sys
path, section, key, value = sys.argv[1:5]
cp = configparser.ConfigParser()
cp.read(path)
if not cp.has_section(section):
    cp.add_section(section)
cp.set(section, key, value)
with open(path, "w") as fh:
    cp.write(fh)
PY
  else
    warn "python3 not found — set [$section] $key = $value in $CFG by hand."
  fi
}

# ── 1. .env (Postgres password + RTP range) ───────────────────────────────────
say "Configuring $ENV_FILE"
[ -f "$ENV_FILE" ] || { cp .env.example "$ENV_FILE"; ok "created $ENV_FILE from .env.example"; }

PG_PW="${POSTGRES_PASSWORD:-}"
if [ -z "$PG_PW" ]; then
  if grep -qE '^POSTGRES_PASSWORD=.+$' "$ENV_FILE" && ! grep -qE '^POSTGRES_PASSWORD=(changeme|password|)$' "$ENV_FILE"; then
    ok "POSTGRES_PASSWORD already set in $ENV_FILE"
  else
    read -rsp "   Enter a strong PostgreSQL password: " PG_PW; echo
    [ -n "$PG_PW" ] || die "A Postgres password is required."
  fi
fi
if [ -n "$PG_PW" ]; then
  if grep -qE '^POSTGRES_PASSWORD=' "$ENV_FILE"; then
    sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PG_PW}|" "$ENV_FILE"
  else
    printf 'POSTGRES_PASSWORD=%s\n' "$PG_PW" >> "$ENV_FILE"
  fi
  ok "POSTGRES_PASSWORD set"
fi

RTP_RANGE="${RTP_RANGE:-16384-16584}"
if grep -qE '^RTP_PORT_RANGE=' "$ENV_FILE"; then
  sed -i "s|^RTP_PORT_RANGE=.*|RTP_PORT_RANGE=${RTP_RANGE}|" "$ENV_FILE"
else
  printf 'RTP_PORT_RANGE=%s\n' "$RTP_RANGE" >> "$ENV_FILE"
fi
RTP_LO="${RTP_RANGE%-*}"; RTP_HI="${RTP_RANGE#*-}"
ok "RTP range = $RTP_RANGE"

# ── 2. gencall.cfg — RTP window ───────────────────────────────────────────────
say "Configuring $CFG (RTP window must match the firewall)"
set_cfg sip min_rtp_port "$RTP_LO"
set_cfg sip max_rtp_port "$RTP_HI"
ok "[sip] min/max_rtp_port = $RTP_LO / $RTP_HI"

# ── 2b. HTTPS — self-signed cert (idempotent) ─────────────────────────────────
# Cert lives in gencall/etc/certs/ (bind-mounted into the container). The config
# references the CONTAINER path. Key is group-readable (mode 640, group root) so
# the non-root container user (UID 1001, group root) can read it without being
# world-readable. We do NOT regenerate an existing cert.
if [ "$USE_TLS" = true ]; then
  command -v openssl >/dev/null 2>&1 || die "GENCALL_TLS=https needs openssl on the host."
  HOST_CERT_DIR="gencall/etc/certs"
  CERT="$HOST_CERT_DIR/gencall.crt"
  KEY="$HOST_CERT_DIR/gencall.key"
  CTR_CERT="/opt/gencall/gencall/etc/certs/gencall.crt"
  CTR_KEY="/opt/gencall/gencall/etc/certs/gencall.key"
  mkdir -p "$HOST_CERT_DIR"
  if [ -f "$CERT" ] && [ -f "$KEY" ]; then
    ok "TLS cert already present ($CERT) — reusing"
  else
    HOST_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -n "$HOST_ADDR" ] || HOST_ADDR="$(hostname -f 2>/dev/null || hostname)"
    if printf '%s' "$HOST_ADDR" | grep -qE '^[0-9]+(\.[0-9]+){3}$'; then SAN="IP:$HOST_ADDR"; else SAN="DNS:$HOST_ADDR"; fi
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -keyout "$KEY" -out "$CERT" -subj "/CN=$HOST_ADDR" \
      -addext "subjectAltName=$SAN" >/dev/null 2>&1 \
      || die "openssl failed to generate the self-signed cert"
    ok "generated self-signed TLS cert (CN=$HOST_ADDR, $SAN, 3650d)"
  fi
  # Group root + 640 so the container's non-root user (group root) can read the
  # bind-mounted key, without it being world-readable.
  chmod 644 "$CERT"; chmod 640 "$KEY"; chgrp 0 "$KEY" "$CERT" 2>/dev/null || true
  set_cfg web ssl true
  set_cfg web ssl_cert "$CTR_CERT"
  set_cfg web ssl_key "$CTR_KEY"
  ok "TLS enabled in config ([web] ssl=true, cert=$CTR_CERT)"
fi

# Inbound trust whitelist is no longer set here — configure it from the
# controller console (Configuration → Inbound Trust), which pushes it to every
# worker at runtime. The HOST FIREWALL remains the real boundary (see docs).

# ── 3. Build the image (SIPp compiled from source) ────────────────────────────
say "Building the worker image (compiles SIPp from source — first build is slow)"
$COMPOSE build
ok "image built"

# ── 4. Start the stack in the correct order ───────────────────────────────────
say "Starting PostgreSQL first, then the worker + controller"
$COMPOSE up -d postgres
printf '   waiting for PostgreSQL to accept connections'
for _ in $(seq 1 60); do
  if $COMPOSE exec -T postgres pg_isready -q >/dev/null 2>&1; then echo; ok "PostgreSQL ready"; break; fi
  printf '.'; sleep 2
done
$COMPOSE up -d gencall controller
ok "worker + controller started (DB migrations apply automatically at worker boot)"

# ── 5. Health checks ──────────────────────────────────────────────────────────
say "Health checks"
sleep 4
if $COMPOSE exec -T gencall sipp -v >/dev/null 2>&1; then
  ok "SIPp present in the worker image: $($COMPOSE exec -T gencall sipp -v 2>/dev/null | head -1)"
else
  warn "Could not run 'sipp -v' in the worker — check '$COMPOSE logs gencall'."
fi
# tcpdump for on-demand pcap capture (Trace). It is installed in the worker
# image (gencall/Dockerfile). NOTE: the worker container runs non-root, so to
# actually capture, the container must also hold NET_RAW — add to the worker
# service in your compose file:  cap_add: ["NET_RAW", "NET_ADMIN"]  (without it
# Trace capture fails cleanly with a 503, it does not crash). Here we only
# verify the binary is present in the image.
if $COMPOSE exec -T gencall tcpdump --version >/dev/null 2>&1; then
  ok "tcpdump present in the worker image (Trace also needs the container's cap_add NET_RAW to capture)"
else
  warn "tcpdump not found in the worker image — on-demand Trace capture will be unavailable; rebuild the image ('$COMPOSE build') or check '$COMPOSE logs gencall'."
fi
curl -fskS "${SCHEME}://127.0.0.1:8080/api/health" >/dev/null 2>&1 && ok "worker  /api/health OK" || warn "worker health not responding yet — check '$COMPOSE logs gencall'"
curl -fskS "${SCHEME}://127.0.0.1:8090/api/health" >/dev/null 2>&1 && ok "controller /api/health OK" || warn "controller health not responding yet — check '$COMPOSE logs controller'"

# ── 5b. Initial console login account ─────────────────────────────────────────
# The console (served by the CONTROLLER) now requires a login. Create one admin
# account in the controller's DB (idempotent — only if none exists). Username:
# GENCALL_ADMIN_USER (default admin). Password: GENCALL_ADMIN_PASSWORD, else a
# strong random one, printed once below. Passed via GENCALL_USER_PASSWORD env
# (NOT argv) so it never lands in ps. Min length 8.
say "Creating initial console account (controller)"
ADMIN_USER="${GENCALL_ADMIN_USER:-admin}"
if $COMPOSE exec -T controller gencall users list 2>/dev/null | grep -qE 'username[[:space:]]+role'; then
  ok "console account(s) already exist — leaving them untouched"
  ADMIN_PASSWORD=""
else
  ADMIN_PASSWORD="${GENCALL_ADMIN_PASSWORD:-}"
  GEN_PW=0
  if [ -z "$ADMIN_PASSWORD" ]; then
    if command -v openssl >/dev/null 2>&1; then
      ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)"; GEN_PW=1
    else
      ADMIN_PASSWORD="$($COMPOSE exec -T controller python3 -c 'import secrets;print(secrets.token_urlsafe(16))' 2>/dev/null | tr -d '\r')"; GEN_PW=1
    fi
  fi
  if [ -n "$ADMIN_PASSWORD" ] && $COMPOSE exec -T -e GENCALL_USER_PASSWORD="$ADMIN_PASSWORD" \
       controller gencall users create "$ADMIN_USER" >/dev/null 2>&1; then
    ok "console account '$ADMIN_USER' created"
  else
    warn "could not create the console account now — create one later: $COMPOSE exec controller gencall users create $ADMIN_USER"
    ADMIN_PASSWORD=""
  fi
fi

# ── 6. Where to go next ───────────────────────────────────────────────────────
say "Almost there — two things YOU must still do"
if [ -n "${ADMIN_PASSWORD:-}" ]; then
  echo "   +-- CONSOLE LOGIN (shown once) -----------------------------------------"
  echo "   |  username:  ${ADMIN_USER}"
  echo "   |  password:  ${ADMIN_PASSWORD}"
  [ "${GEN_PW:-0}" = 1 ] && echo "   |  (auto-generated — save it now; it is NOT stored anywhere)"
  echo "   +----------------------------------------------------------------------"
  echo
fi
cat <<EOF
   1) API KEY: the worker minted an admin key on first boot. Grab it with:
        $COMPOSE logs gencall | grep -A1 'X-API-Key'
      Save it — it is shown only once. Manage keys: '$COMPOSE exec gencall gencall keys list'.

   2) FIREWALL (the REAL trust boundary — not done automatically to avoid locking
      you out): apply the nftables OR ufw rules in docs/deploy/loop-runner.md §2,
      restricting UDP/5060 + ${RTP_LO}-${RTP_HI} to the MADA whitelist (${MADA:-<set this>}).

   Then prove the real-SIPp call path on the box:
        ./deploy/smoke-loopback.sh
   And open the console:  ${SCHEME}://<box>:8090/console/  → the Loops page.
EOF
ok "install.sh finished"

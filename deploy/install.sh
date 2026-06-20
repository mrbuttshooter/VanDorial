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
curl -fsS http://127.0.0.1:8080/api/health >/dev/null 2>&1 && ok "worker  /api/health OK" || warn "worker health not responding yet — check '$COMPOSE logs gencall'"
curl -fsS http://127.0.0.1:8090/api/health >/dev/null 2>&1 && ok "controller /api/health OK" || warn "controller health not responding yet — check '$COMPOSE logs controller'"

# ── 6. Where to go next ───────────────────────────────────────────────────────
say "Almost there — two things YOU must still do"
cat <<EOF
   1) API KEY: the worker minted an admin key on first boot. Grab it with:
        $COMPOSE logs gencall | grep -A1 'X-API-Key'
      Save it — it is shown only once. Manage keys: '$COMPOSE exec gencall gencall keys list'.

   2) FIREWALL (the REAL trust boundary — not done automatically to avoid locking
      you out): apply the nftables OR ufw rules in docs/deploy/loop-runner.md §2,
      restricting UDP/5060 + ${RTP_LO}-${RTP_HI} to the MADA whitelist (${MADA:-<set this>}).

   Then prove the real-SIPp call path on the box:
        ./deploy/smoke-loopback.sh
   And open the console:  http://<box>:8090/console/  → the Loops page.
EOF
ok "install.sh finished"

#!/usr/bin/env bash
#
# enable-https.sh — switch an installed GenCall box from HTTP to HTTPS (or back).
#
# The app already serves TLS natively (uvicorn) when [web] ssl=true and
# ssl_cert/ssl_key are set. This script does the operator side, idempotently:
#   1. auto-detects the config path, venv/base dir, service user and port from
#      the running systemd unit (no hard-coded paths),
#   2. generates a self-signed cert (2048-bit RSA, 3650d, host IP/DNS in SAN)
#      into <base>/certs/ — REUSED if one already exists (never overwritten),
#   3. sets [web] ssl=true + ssl_cert/ssl_key via configparser (no dup section,
#      comments elsewhere preserved by section),
#   4. restarts the worker and does a TLS-aware health check.
#
# Usage:
#   sudo bash enable-https.sh                 # enable HTTPS (self-signed)
#   sudo bash enable-https.sh --off           # revert to plain HTTP
#   sudo GENCALL_CFG=/path/gencall.cfg bash enable-https.sh   # explicit config
#   sudo GENCALL_TLS_HOST=noc.example.com bash enable-https.sh  # cert CN/SAN
#
# Self-signed certs trip browser warnings (expected on an internal tool); point
# GENCALL_TLS_HOST at the name/IP you actually browse to so the SAN matches.
set -euo pipefail

say()  { printf '   [https] %s\n' "$*"; }
ok()   { printf '   \033[32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '   \033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo) — it writes certs, edits config, restarts the service"

MODE="on"
[ "${1:-}" = "--off" ] && MODE="off"

# ── 1. Locate the service + its real paths ───────────────────────────────────
UNIT=""
for u in gencall-worker gencall gencall-controller; do
  if systemctl cat "${u}.service" >/dev/null 2>&1; then UNIT="$u"; break; fi
done
[ -n "$UNIT" ] || die "no gencall systemd unit found (looked for gencall-worker/gencall/gencall-controller)"
say "service: ${UNIT}.service"

# ExecStart → venv bin path + --port. systemd renders ExecStart as a struct;
# --value gives the argv. Grab the gencall-server path and any --port N.
EXEC="$(systemctl show -p ExecStart --value "$UNIT" 2>/dev/null || true)"
VENV_BIN="$(printf '%s\n' "$EXEC" | grep -oE '/[^ ]*/venv/bin/gencall-server' | head -1 || true)"
if [ -n "$VENV_BIN" ]; then
  BASE_DIR="$(cd "$(dirname "$VENV_BIN")/../.." && pwd)"   # .../venv/bin -> base
else
  BASE_DIR="/opt/gencall"
  warn "could not parse venv from ExecStart; assuming base $BASE_DIR"
fi
PORT="$(printf '%s\n' "$EXEC" | grep -oE -- '--port[= ]+[0-9]+' | grep -oE '[0-9]+' | head -1 || true)"
[ -n "$PORT" ] || PORT=8000

# Config path: explicit override > unit's GENCALL_CONFIG env > common defaults.
CFG="${GENCALL_CFG:-}"
if [ -z "$CFG" ]; then
  CFG="$(systemctl show -p Environment --value "$UNIT" 2>/dev/null \
        | tr ' ' '\n' | sed -n 's/^GENCALL_CONFIG=//p' | head -1 || true)"
fi
for cand in "$CFG" "$BASE_DIR/gencall/etc/gencall.cfg" "$BASE_DIR/etc/gencall.cfg" /etc/gencall/gencall.cfg; do
  [ -n "$cand" ] && [ -f "$cand" ] && { CFG="$cand"; break; }
done
[ -n "$CFG" ] && [ -f "$CFG" ] || die "could not find gencall.cfg — pass it with GENCALL_CFG=/path/gencall.cfg"
say "config:  $CFG"

# Service user (for cert ownership). Default gencall.
GC_USER="$(systemctl show -p User --value "$UNIT" 2>/dev/null || true)"
[ -n "$GC_USER" ] || GC_USER="gencall"

# ── configparser-backed config setter (same idiom as the installers) ─────────
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

# ── 2. --off: revert to HTTP and restart ─────────────────────────────────────
if [ "$MODE" = "off" ]; then
  set_cfg web ssl false
  ok "TLS disabled in config ([web] ssl=false) — cert files left in place"
  say "restarting ${UNIT}…"
  systemctl restart "$UNIT"; sleep 2
  systemctl is-active --quiet "$UNIT" && ok "${UNIT} active (HTTP on :$PORT)" \
    || die "${UNIT} failed — check: journalctl -u $UNIT -n 50"
  exit 0
fi

# ── 3. Generate (or reuse) the self-signed cert ──────────────────────────────
command -v openssl >/dev/null 2>&1 || die "openssl not on PATH (needed to generate the cert)"
CERT_DIR="$BASE_DIR/certs"
CERT="$CERT_DIR/gencall.crt"
KEY="$CERT_DIR/gencall.key"
# The non-root service user must be able to TRAVERSE this dir and READ the key,
# or uvicorn dies with PermissionError and systemd crash-loops the worker. Own
# the dir by the service user (750 lets the owner traverse; others can't peek).
mkdir -p "$CERT_DIR"
chown "$GC_USER":"$GC_USER" "$CERT_DIR" 2>/dev/null || true
chmod 750 "$CERT_DIR"

if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  ok "TLS cert already present ($CERT) — reusing (delete it to regenerate)"
else
  HOST_ADDR="${GENCALL_TLS_HOST:-}"
  if [ -z "$HOST_ADDR" ]; then
    HOST_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -n "$HOST_ADDR" ] || HOST_ADDR="$(hostname -f 2>/dev/null || hostname)"
  fi
  if printf '%s' "$HOST_ADDR" | grep -qE '^[0-9]+(\.[0-9]+){3}$'; then
    SAN="IP:$HOST_ADDR"
  else
    SAN="DNS:$HOST_ADDR"
  fi
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$KEY" -out "$CERT" \
    -subj "/CN=$HOST_ADDR" -addext "subjectAltName=$SAN" >/dev/null 2>&1 \
    || die "openssl failed to generate the self-signed cert"
  ok "generated self-signed cert (CN=$HOST_ADDR, $SAN, 3650d)"
fi
chmod 600 "$KEY"; chmod 644 "$CERT"
chown "$GC_USER":"$GC_USER" "$KEY" "$CERT" 2>/dev/null || warn "could not chown cert to $GC_USER (continuing)"

# Pre-flight: PROVE the service user can actually read the key BEFORE we flip the
# config + restart. If it can't, leave the box on HTTP (don't crash-loop it).
if [ "$GC_USER" != "root" ] && command -v runuser >/dev/null 2>&1; then
  if ! runuser -u "$GC_USER" -- test -r "$KEY" 2>/dev/null; then
    die "service user '$GC_USER' cannot read $KEY — fix perms/ownership; config left on HTTP"
  fi
elif [ "$GC_USER" != "root" ]; then
  if ! sudo -u "$GC_USER" test -r "$KEY" 2>/dev/null; then
    die "service user '$GC_USER' cannot read $KEY — fix perms/ownership; config left on HTTP"
  fi
fi

# ── 4. Point the config at it and restart ────────────────────────────────────
set_cfg web ssl true
set_cfg web ssl_cert "$CERT"
set_cfg web ssl_key "$KEY"
ok "TLS enabled in config ([web] ssl=true, cert=$CERT)"

say "restarting ${UNIT}  (running loops will stop and auto-resume)…"
systemctl restart "$UNIT"; sleep 2
systemctl is-active --quiet "$UNIT" || die "${UNIT} failed to start — check: journalctl -u $UNIT -n 50"

# ── 5. TLS-aware health check ────────────────────────────────────────────────
if curl -fsSk "https://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  ok "HTTPS health check passed"
else
  warn "HTTPS health check didn't pass yet — the worker may still be booting; verify: curl -k https://127.0.0.1:${PORT}/api/health"
fi

HOST_SHOW="${GENCALL_TLS_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
ok "done — console now at: https://${HOST_SHOW:-<host>}:${PORT}/console"
say "self-signed cert: browsers will warn once; accept it (or import $CERT)."

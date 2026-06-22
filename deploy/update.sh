#!/usr/bin/env bash
# ============================================================================
# GenCall air-gapped UPDATE script (cy214 native install).
#
# Drops a new code bundle over the live install WITHOUT clobbering anything
# operator-owned. Safe to run every time you ship an update.
#
#   PRESERVED (never overwritten/deleted):
#     - etc/gencall.cfg        (live config: sipp path, MADA settings, allowlists)
#     - venv / .venv           (editable install; replacing .py is enough)
#     - *.db / *.sqlite*       (databases)
#     - logs/
#   The DB dir /opt/gencall/data is OUTSIDE the package tree, so untouched anyway.
#   The full sale-codes deck (scripts/data/sale_codes.csv) IS bundled now and is
#   intentionally deployed (overwrites the box copy) so all zones are available.
#
# Usage:
#   sudo bash update.sh [/path/to/VanDorial-*.tar.gz|.zip]
#     - no arg  -> auto-picks the newest /tmp/VanDorial*.{tar.gz,tgz,zip}
#   Options (env):
#     GENCALL_DIR=/opt/gencall/gencall   install dir   (default shown)
#     PRUNE=1                            also delete stale code files removed
#                                        upstream (rsync --delete; excludes still
#                                        protect config/data/venv/db/logs)
# ============================================================================
set -euo pipefail

INSTALL_DIR="${GENCALL_DIR:-/opt/gencall/gencall}"
ARCHIVE="${1:-}"

say() { printf '[*] %s\n' "$*"; }
ok()  { printf '[\xe2\x9c\x93] %s\n' "$*"; }
die() { printf '[!] %s\n' "$*" >&2; exit 1; }

# 1) locate the bundle ------------------------------------------------------
if [[ -z "$ARCHIVE" ]]; then
  ARCHIVE=$(ls -t /tmp/VanDorial*.tar.gz /tmp/VanDorial*.tgz /tmp/VanDorial*.zip 2>/dev/null | head -1 || true)
fi
[[ -n "$ARCHIVE" && -f "$ARCHIVE" ]] || die "no bundle found — pass a path or drop VanDorial-*.tar.gz in /tmp"
say "bundle:  $ARCHIVE"
[[ -d "$INSTALL_DIR" ]] || die "install dir not found: $INSTALL_DIR (set GENCALL_DIR=...)"

# 2) unpack to a throwaway dir ---------------------------------------------
WORK=$(mktemp -d /tmp/gencall_update.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
say "unpack:  $WORK"
case "$ARCHIVE" in
  *.zip)          command -v unzip >/dev/null || die "unzip not installed (use a .tar.gz bundle)"; unzip -q "$ARCHIVE" -d "$WORK" ;;
  *.tar.gz|*.tgz) tar -xzf "$ARCHIVE" -C "$WORK" ;;
  *)              die "unknown archive type: $ARCHIVE (want .tar.gz or .zip)" ;;
esac

# 3) find the gencall package inside the unpacked tree (any top-dir name) ---
PKG_MARKER=$(find "$WORK" -type f -path '*/gencall/core/sipp_engine.py' | head -1 || true)
[[ -n "$PKG_MARKER" ]] || die "could not find the gencall package inside the bundle"
SRC=$(cd "$(dirname "$PKG_MARKER")/.." && pwd)     # .../gencall
say "source:  $SRC"

# 4) back up the live config BEFORE touching anything ----------------------
CFG="$INSTALL_DIR/etc/gencall.cfg"
if [[ -f "$CFG" ]]; then
  BK="$CFG.bak.$(date +%Y%m%d-%H%M%S)"
  cp -a "$CFG" "$BK"; say "config backed up -> $BK"
fi

# 5) sync code — excludes keep everything operator-owned safe ---------------
RSYNC_OPTS=(-a)
[[ "${PRUNE:-0}" == "1" ]] && { RSYNC_OPTS+=(--delete); say "PRUNE on: stale code files will be removed"; }
say "syncing code -> $INSTALL_DIR"
# NOTE: we deliberately do NOT --exclude 'data' anymore: the full sale-codes deck
# (scripts/data/sale_codes.csv) is now bundled and SHOULD overwrite the box's copy.
# The database lives at /opt/gencall/data (OUTSIDE this install dir, never touched),
# and any stray *.db/*.sqlite* inside the tree is still excluded below.
rsync "${RSYNC_OPTS[@]}" \
  --exclude 'etc' \
  --exclude 'venv' --exclude '.venv' \
  --exclude '*.db' --exclude '*.sqlite*' \
  --exclude 'logs' \
  "$SRC"/ "$INSTALL_DIR"/

# clear stale bytecode so a removed/renamed module can't shadow the new code
find "$INSTALL_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 5b) disk-hygiene hardening (idempotent) — BEFORE the restart so the worker
# boots with the capped retention value. Caps journald + [retention], installs
# the /tmp sweep cron. Sourced from the unpacked bundle (update.sh doesn't sync
# deploy/ into the install dir).
HARDEN="$(dirname "$SRC")/deploy/harden-disk.sh"
if [ -f "$HARDEN" ]; then
  say "applying disk-hygiene hardening"
  GENCALL_CFG="$INSTALL_DIR/etc/gencall.cfg" bash "$HARDEN" || say "disk hardening had a problem (non-fatal)"
fi

# 5c) ensure the console account + TLS cert exist (idempotent — NEVER clobber) --
# A fresh code drop may introduce the console-login requirement / TLS option. We
# make sure ONE admin account and (if TLS is configured) the cert file exist, but
# we DO NOT reset passwords or regenerate certs on update. DB settings + the venv
# are read from the live config / systemd unit so they match what the worker uses.
CFG="$INSTALL_DIR/etc/gencall.cfg"
BASE_DIR="$(dirname "$INSTALL_DIR")"            # e.g. /opt/gencall (venv + certs live here)
GC_BIN="$BASE_DIR/venv/bin/gencall"
[ -x "$GC_BIN" ] || GC_BIN="$INSTALL_DIR/venv/bin/gencall"
# Pull GENCALL_* env (DB engine/URL) straight from the live worker unit so the CLI
# touches the SAME database the service does.
UNIT_ENV=()
for u in gencall-worker gencall; do
  if systemctl cat "${u}.service" >/dev/null 2>&1; then
    while IFS= read -r _e; do UNIT_ENV+=("$_e"); done < <(
      systemctl cat "${u}.service" 2>/dev/null \
        | sed -n 's/^Environment=\(GENCALL_[A-Z_]*=.*\)$/\1/p')
    break
  fi
done
if [ -x "$GC_BIN" ] && [ -f "$CFG" ]; then
  # Console account: create 'admin' only if NO console user exists yet.
  if env GENCALL_CONFIG="$CFG" "${UNIT_ENV[@]}" "$GC_BIN" users list 2>/dev/null | grep -qE 'username[[:space:]]+role'; then
    say "console account already present — not touched"
  else
    ADMIN_USER="${GENCALL_ADMIN_USER:-admin}"
    ADMIN_PASSWORD="${GENCALL_ADMIN_PASSWORD:-}"
    GEN_PW=0
    if [ -z "$ADMIN_PASSWORD" ] && command -v openssl >/dev/null 2>&1; then
      ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)"; GEN_PW=1
    fi
    if [ -n "$ADMIN_PASSWORD" ] && env GENCALL_CONFIG="$CFG" "${UNIT_ENV[@]}" \
         GENCALL_USER_PASSWORD="$ADMIN_PASSWORD" "$GC_BIN" users create "$ADMIN_USER" >/dev/null 2>&1; then
      ok "created initial console account '$ADMIN_USER'"
      echo "    +-- CONSOLE LOGIN (shown once) ---------------------------------------"
      echo "    |  username:  ${ADMIN_USER}"
      echo "    |  password:  ${ADMIN_PASSWORD}"
      [ "$GEN_PW" = 1 ] && echo "    |  (auto-generated — save it now; it is NOT stored anywhere)"
      echo "    +---------------------------------------------------------------------"
    else
      say "no console account and could not create one — make one later: $GC_BIN users create admin"
    fi
  fi
  # TLS cert: if config has [web] ssl=true but the cert file is missing, generate
  # it (idempotent). NEVER overwrite an existing cert.
  if grep -qiE '^[[:space:]]*ssl[[:space:]]*=[[:space:]]*(true|1|yes)' "$CFG" 2>/dev/null; then
    CERT="$BASE_DIR/certs/gencall.crt"; KEY="$BASE_DIR/certs/gencall.key"
    if [ -f "$CERT" ] && [ -f "$KEY" ]; then
      say "TLS cert present — not regenerated"
    elif command -v openssl >/dev/null 2>&1; then
      mkdir -p "$BASE_DIR/certs"
      # Own the dir by the service user so it can TRAVERSE it to read the key
      # (else uvicorn dies PermissionError and systemd crash-loops the worker).
      chown gencall:gencall "$BASE_DIR/certs" 2>/dev/null || true
      chmod 750 "$BASE_DIR/certs"
      HOST_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
      [ -n "$HOST_ADDR" ] || HOST_ADDR="$(hostname -f 2>/dev/null || hostname)"
      if printf '%s' "$HOST_ADDR" | grep -qE '^[0-9]+(\.[0-9]+){3}$'; then SAN="IP:$HOST_ADDR"; else SAN="DNS:$HOST_ADDR"; fi
      if openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
           -keyout "$KEY" -out "$CERT" -subj "/CN=$HOST_ADDR" \
           -addext "subjectAltName=$SAN" >/dev/null 2>&1; then
        chmod 600 "$KEY"; chmod 644 "$CERT"
        chown gencall:gencall "$KEY" "$CERT" 2>/dev/null || true
        ok "generated missing TLS cert ($CERT)"
      else
        say "[web] ssl=true but cert generation failed — check openssl"
      fi
    fi
  fi
fi

# 6) restart the worker (auto-detect the unit name) ------------------------
UNIT=""
for u in gencall-worker gencall gencall-controller; do
  # `systemctl cat` exits 0 iff a unit file exists (active or not) — more robust
  # than grepping list-unit-files, whose output formatting/truncation made the
  # match miss gencall-worker.service on cy214.
  if systemctl cat "${u}.service" >/dev/null 2>&1; then UNIT="$u"; break; fi
done
if [[ -n "$UNIT" ]]; then
  say "restarting $UNIT  (running loops will stop and reconcile)"
  systemctl restart "$UNIT"
  sleep 2
  systemctl is-active --quiet "$UNIT" && ok "$UNIT active" || die "$UNIT failed — check: journalctl -u $UNIT -n 50"
else
  printf '[!] no gencall systemd unit found — restart the worker manually\n'
fi

# 7) health check -----------------------------------------------------------
say "health:"
curl -fsS http://127.0.0.1:8000/api/health && echo || printf '\n[!] health check failed (worker may still be starting)\n'

ok "updated from $(basename "$ARCHIVE")"

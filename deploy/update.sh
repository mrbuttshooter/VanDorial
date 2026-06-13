#!/usr/bin/env bash
# ============================================================================
# GenCall air-gapped UPDATE script (cy214 native install).
#
# Drops a new code bundle over the live install WITHOUT clobbering anything
# operator-owned. Safe to run every time you ship an update.
#
#   PRESERVED (never overwritten/deleted):
#     - etc/gencall.cfg        (live config: sipp path, MADA settings, allowlists)
#     - scripts/data/          (the proprietary sale_codes deck — NOT in the bundle)
#     - venv / .venv           (editable install; replacing .py is enough)
#     - *.db / *.sqlite*       (databases)
#     - logs/
#   The DB dir /opt/gencall/data is OUTSIDE the package tree, so untouched anyway.
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
rsync "${RSYNC_OPTS[@]}" \
  --exclude 'etc' \
  --exclude 'data' \
  --exclude 'venv' --exclude '.venv' \
  --exclude '*.db' --exclude '*.sqlite*' \
  --exclude 'logs' \
  "$SRC"/ "$INSTALL_DIR"/

# clear stale bytecode so a removed/renamed module can't shadow the new code
find "$INSTALL_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

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

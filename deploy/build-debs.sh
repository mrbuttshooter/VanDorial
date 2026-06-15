#!/usr/bin/env bash
#
# Fetch SIPp + Python (venv/pip) and their full dependency CLOSURE as .debs, so an
# OFFLINE box needs NOTHING preinstalled. The .debs land in vendor/debs/, which
# deploy/install-offline.sh then `dpkg -i`s with no internet.
#
# RUN THIS ON AN ONLINE UBUNTU BOX THAT MATCHES THE AIR-GAPPED TARGETS
# (same Ubuntu release + same python3 version as cy213/cy214). apt resolves the
# EXACT versions/deps those boxes need — that matters because python3-venv is
# version-locked to the box's python3, so a generic .deb off the internet would
# fail dpkg on a box at a different patch level.
#
#     sudo ./deploy/build-debs.sh
#     sudo PKGS="sip-tester python3-venv python3-pip" ./deploy/build-debs.sh
#
# Then re-pack the bundle (git archive / tar) — or commit vendor/debs/ — so the
# release carries them. Deploy the resulting bundle only to boxes on the SAME
# Ubuntu/python version you ran this on.
#
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root (needs the apt cache):  sudo $0" >&2; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/vendor/debs"
PKGS="${PKGS:-sip-tester python3-venv python3-pip}"
mkdir -p "$OUT"
. /etc/os-release 2>/dev/null || true

echo "Fetching [$PKGS] + dependency closure"
echo "  for: ${PRETTY_NAME:-this OS} / $(python3 -V 2>&1)  ->  $OUT"
apt-get update -qq || true

# Recursive dependency closure -> package-name lines (column-0); skip virtuals.
DEPS="$(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
        --no-breaks --no-replaces --no-enhances --no-pre-depends $PKGS \
        | grep '^\w' | sort -u)"

cd "$OUT"
# --reinstall so deps already present on THIS box are still downloaded (the target
# box may be missing them). Falls back to a plain download if --reinstall balks.
apt-get download --reinstall $DEPS 2>/dev/null || apt-get download $DEPS 2>/dev/null || true

echo
echo "Done: $(ls "$OUT"/*.deb 2>/dev/null | wc -l) .debs in $OUT"
echo "python3-venv here matches THIS box's python3 — deploy this bundle only to"
echo "boxes on the same Ubuntu/python version."

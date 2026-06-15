#!/usr/bin/env bash
#
# Build / refresh vendor/wheelhouse for OFFLINE (air-gapped) installs.
#
# Run this ON AN ONLINE BOX whose Python version + OS match the AIR-GAPPED targets
# (e.g. Ubuntu 22.04 / Python 3.10 — the same as cy213/cy214). It downloads every
# Python dependency (transitive + the build backend) as wheels into
# vendor/wheelhouse/, which deploy/install-offline.sh then installs with --no-index
# (no internet). Commit the result (or tar it into the bundle) so the REST of the
# air-gapped boxes install with zero manual steps.
#
#     ./deploy/build-wheelhouse.sh            # uses python3
#     PYTHON=python3.10 ./deploy/build-wheelhouse.sh
#
# Native wheels (uvloop, pydantic_core, psycopg2, …) are locked to the Python ABI
# (cp3X), so build on the SAME Python minor version the target boxes run.
#
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/vendor/wheelhouse"
PY="${PYTHON:-python3}"

command -v "$PY" >/dev/null 2>&1 || { echo "ERROR: '$PY' not found" >&2; exit 1; }
[ -f "$REPO/requirements.txt" ] || { echo "ERROR: requirements.txt not found in $REPO" >&2; exit 1; }

echo "Building wheelhouse for $("$PY" -V 2>&1)  ->  $OUT"
mkdir -p "$OUT"

# App dependencies (requirements.txt) + the build backend so 'pip install -e .'
# resolves offline too (install-offline.sh uses --no-build-isolation).
"$PY" -m pip download -d "$OUT" -r "$REPO/requirements.txt"
"$PY" -m pip download -d "$OUT" pip setuptools wheel

echo
echo "Done: $(ls "$OUT"/*.whl 2>/dev/null | wc -l) wheels in $OUT  (for $("$PY" -V 2>&1))."
echo "Re-pack the bundle (git archive / tar) so install-offline.sh on the air-gapped"
echo "boxes picks them up with --no-index."

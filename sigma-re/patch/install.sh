#!/usr/bin/env bash
#
# install.sh - deploy the sigma RTP-leak hotfix onto the server.
#
# It copies sigma_patch.py into the site-packages of the SAME Python that runs sigma,
# and drops sigma_patch.pth so the patch auto-loads on every interpreter start in that
# environment (no edits to the compiled sigma package required).
#
# Usage:
#   ./install.sh                      # auto-detect a python that can import sigma
#   SIGMA_PYTHON=/opt/sigma/venv/bin/python ./install.sh   # or point it explicitly
#
# After install, restart the sigma service so workers pick up the patch.
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== sigma RTP-leak hotfix installer =="

# 1. Locate the interpreter that can import sigma.
PYTHON="${SIGMA_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sigma' >/dev/null 2>&1; then
      PYTHON="$c"; break
    fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "ERROR: could not find a python able to 'import sigma'."
  echo "       Re-run as: SIGMA_PYTHON=/path/to/venv/bin/python $0"
  exit 1
fi
echo "Using interpreter: $("$PYTHON" -c 'import sys; print(sys.executable)')"

# 2. Resolve its site-packages (purelib) directory.
SITE="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
if [ -z "$SITE" ] || [ ! -d "$SITE" ]; then
  echo "ERROR: could not resolve site-packages dir (got: '$SITE')"; exit 1
fi
echo "Target site-packages: $SITE"

# 3. Install module + auto-loader.
cp -f "$HERE/sigma_patch.py" "$SITE/sigma_patch.py"
printf 'import sigma_patch\n' > "$SITE/sigma_patch.pth"
echo "Installed: $SITE/sigma_patch.py"
echo "Installed: $SITE/sigma_patch.pth  (auto-loads the patch)"

# 4. Verify the hook installs and (best-effort) that RTP is patchable.
echo "== verification =="
"$PYTHON" "$HERE/verify.py" || {
  echo "WARNING: verification reported a problem - see output above and the patch log."
}

echo
echo "Done. Now restart the sigma service/workers so they load the patch, e.g.:"
echo "    systemctl restart sigma        # or your service name"
echo
echo "First-time tip: deploy in DRY-RUN first to confirm it only targets owned sockets:"
echo "    export SIGMA_PATCH_DRY_RUN=1   # in the service environment, then restart"
echo "    tail -f \$SIGMA_PATCH_LOG       # default /var/log/sigma_patch.log or \$TMPDIR"
echo "Once the dry-run log looks right, remove SIGMA_PATCH_DRY_RUN and restart again."
echo
echo "Rollback:  rm '$SITE/sigma_patch.py' '$SITE/sigma_patch.pth' && systemctl restart sigma"

#!/usr/bin/env bash
# ============================================================================
# GenCall disk-hygiene hardening — idempotent, run as root.
#
# Called automatically by deploy/install-offline.sh (fresh install) and
# deploy/update.sh (update) so every install/update bounds disk usage without
# the operator remembering to. A worker doing ~400k call_records rows/day on a
# small disk WILL fill it otherwise (observed: a 9.8 GB box hit 100%, DB writes
# then 500'd). This sets the guards that keep that from happening:
#
#   1. Caps journald (SystemMaxUse) so the system journal can't grow unbounded.
#   2. Caps [retention] call_records_days at 7 in the live config (never RAISES
#      it — a lower operator value is respected) and ensures the disk-saving loop
#      knob [loops] sipp_trace_err exists (off).
#   3. Installs a daily cron that sweeps ORPHANED SIPp temp logs + old release
#      bundles from /tmp (only files >1 day old, so active logs are untouched).
#
# Env:
#   GENCALL_CFG   path to gencall.cfg (caller passes the live one)
#   JOURNAL_MAX   journald SystemMaxUse (default 200M)
# ============================================================================
set -euo pipefail

CFG="${GENCALL_CFG:-/opt/gencall/gencall/etc/gencall.cfg}"
JOURNAL_MAX="${JOURNAL_MAX:-200M}"
say() { printf '   [disk] %s\n' "$*"; }

# 1) Cap journald ------------------------------------------------------------
if [ -d /etc/systemd ]; then
  mkdir -p /etc/systemd/journald.conf.d
  cat > /etc/systemd/journald.conf.d/gencall-cap.conf <<EOF
[Journal]
SystemMaxUse=${JOURNAL_MAX}
EOF
  systemctl restart systemd-journald 2>/dev/null || true
  say "journald capped at ${JOURNAL_MAX}"
fi

# 2) Harden the config (cap retention, ensure trace-err is off) ---------------
# Uses python3's configparser (always present — GenCall needs python3 anyway).
# Preserves a LOWER operator retention value; only pulls a dangerous-high one
# (e.g. the old 30-day default) down to 7.
if [ -f "$CFG" ]; then
  if python3 - "$CFG" <<'PY'
import configparser, sys
p = sys.argv[1]
c = configparser.ConfigParser()
c.read(p)
def ensure(sec, key, val):
    if not c.has_section(sec):
        c.add_section(sec)
    c.set(sec, key, val)
days = 7
if c.has_option("retention", "call_records_days"):
    try:
        days = min(7, int(c.get("retention", "call_records_days")))
    except ValueError:
        days = 7
ensure("retention", "call_records_days", str(days))
if not c.has_option("loops", "sipp_trace_err"):
    ensure("loops", "sipp_trace_err", "false")
with open(p, "w") as f:
    c.write(f)
PY
  then
    say "config hardened (retention call_records_days<=7, sipp_trace_err present)"
  else
    say "WARN: could not harden $CFG (left unchanged)"
  fi
fi

# 3) Daily sweep of orphaned SIPp temp logs + old bundles in /tmp -------------
# Only deletes files older than 1 day, so a running loop's live logs are spared;
# this is the safety net for crash-orphans that the in-app cleanup missed.
cat > /etc/cron.daily/gencall-tmp-sweep <<'EOF'
#!/bin/sh
# GenCall: reap stale SIPp temp logs + old release bundles (>1 day old).
find /tmp -maxdepth 1 -mtime +1 \( \
     -name 'loop_uac_*_*.log'   -o -name 'loop_uas_*_*.log'  -o \
     -name 'gencall_uac_rtp_*'  -o -name 'gencall_sipp_*.csv' -o \
     -name 'gencall_sipp_*.calllog' -o -name 'VanDorial-*.tar.gz' \
   \) -delete 2>/dev/null || true
EOF
chmod 0755 /etc/cron.daily/gencall-tmp-sweep
say "installed /etc/cron.daily/gencall-tmp-sweep"

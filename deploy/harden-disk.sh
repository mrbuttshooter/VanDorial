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
#   2. Caps [retention] call_records_days at 1 in the live config (never RAISES
#      it — a lower operator value is respected) and ensures the disk-saving loop
#      knob [loops] sipp_trace_err exists (off).
#   3. Installs a daily cron that sweeps ORPHANED SIPp temp artifacts from /tmp:
#      the leaked per-run -inf pools (gencall_loop_*.csv, the big ones), rtp
#      scenarios, trace logs, stats + old release bundles. Only files >1 day old
#      AND not held open by a process, so an active loop is never disturbed.
#   4. Grows the root LVM volume into any unused disk/VG space. The Ubuntu
#      autoinstall image leaves the root LV at ~10G even on a bigger disk (and
#      the partition short of the disk end), so boxes ship with GBs stranded.
#      This is non-destructive (grow only) and idempotent — a no-op once full.
#
# Env:
#   GENCALL_CFG       path to gencall.cfg (caller passes the live one)
#   JOURNAL_MAX       journald SystemMaxUse (default 200M)
#   GENCALL_GROW_DISK 1=grow root LVM into free space (default), 0=skip
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
# Preserves a LOWER operator retention value; only pulls a higher one down to 1.
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
days = 1
if c.has_option("retention", "call_records_days"):
    try:
        days = min(1, int(c.get("retention", "call_records_days")))
    except ValueError:
        days = 1
ensure("retention", "call_records_days", str(days))
if not c.has_option("loops", "sipp_trace_err"):
    ensure("loops", "sipp_trace_err", "false")
with open(p, "w") as f:
    c.write(f)
PY
  then
    say "config hardened (retention call_records_days<=1, sipp_trace_err present)"
  else
    say "WARN: could not harden $CFG (left unchanged)"
  fi
fi

# 3) Daily sweep of orphaned SIPp temp artifacts + old bundles in /tmp --------
# The engine writes a fresh per-run SIPp -inf pool file (gencall_loop_*.csv,
# ~17 MB each) on EVERY campaign start / auto-resume / adaptive-pool restart and
# never unlinks the superseded ones, so a busy box leaks tens of GB of them into
# /tmp. The earlier sweep missed that filename (it only knew the logs/stats), so
# we add it here. Two safety rails keep this from ever touching a LIVE loop:
#   * -mtime +1  — only files older than a day, and
#   * fuser      — skip any file a process still holds open.
# The durable node pools live under /tmp/gencall_numbers/ (a subdir, not matched
# here), so regeneration on resume still works after a sweep.
cat > /etc/cron.daily/gencall-tmp-sweep <<'EOF'
#!/bin/sh
# GenCall: reap stale SIPp temp artifacts (>1 day old) not held open by any
# process — generated -inf pools, rtp scenarios, trace logs, stats, old bundles.
find /tmp -maxdepth 1 -mtime +1 \( \
     -name 'gencall_loop_*.csv'     -o -name 'gencall_uac_rtp_*'    -o \
     -name 'loop_uac_*_*.log'       -o -name 'loop_uas_*_*.log'     -o \
     -name 'gencall_sipp_*.csv'     -o -name 'gencall_sipp_*.calllog' -o \
     -name 'VanDorial-*.tar.gz' \
   \) -print 2>/dev/null | while IFS= read -r f; do
     # Still open by a running SIPp/loop? Leave it. (If fuser is absent it
     # returns non-zero and we fall back to deleting the >1-day-old file.)
     fuser -s "$f" 2>/dev/null && continue
     rm -f "$f" 2>/dev/null || true
   done
EOF
chmod 0755 /etc/cron.daily/gencall-tmp-sweep
say "installed /etc/cron.daily/gencall-tmp-sweep"

# 4) Grow the root LVM volume into unused disk / VG space ---------------------
# The fleet ships from the Ubuntu autoinstall image, which leaves the root LV at
# ~10G even on a 20-30G disk AND leaves the LVM partition short of the disk end
# (observed: 30G disk -> 17.3G part -> 10G LV -> 9.8G "/"). Reclaim it all.
# Non-destructive: every operation only GROWS. Idempotent: when the partition
# already fills the disk and the LV already fills the VG, all three steps are
# no-ops. Conservative: anything that isn't the expected single-LVM-root layout
# is left untouched and skipped with a note.
grow_root() {
  [ "${GENCALL_GROW_DISK:-1}" = "1" ] || { say "disk grow disabled (GENCALL_GROW_DISK=0)"; return 0; }
  command -v lvextend >/dev/null 2>&1 || { say "no LVM tools; skip root grow"; return 0; }
  command -v findmnt  >/dev/null 2>&1 || { say "no findmnt; skip root grow"; return 0; }

  local root_src vg lv pv disk partnum free
  root_src=$(findmnt -no SOURCE / 2>/dev/null) || return 0
  case "$root_src" in
    /dev/mapper/*|/dev/dm-*) : ;;
    *) say "root ($root_src) is not LVM; skip auto-grow"; return 0 ;;
  esac

  # Resolve the VG/LV behind "/". If lvs can't read it, this isn't a layout we
  # understand — leave it alone.
  read -r vg lv < <(lvs --noheadings -o vg_name,lv_name "$root_src" 2>/dev/null | awk '{$1=$1; print}')
  [ -n "${vg:-}" ] && [ -n "${lv:-}" ] || { say "could not resolve root VG/LV; skip grow"; return 0; }

  # Grow every partition that backs this VG up to its disk end, then pvresize so
  # the new space lands in the VG. growpart needs cloud-guest-utils; if it's
  # absent we still reclaim whatever is already free in the VG (the bigger win).
  while read -r pv; do
    [ -n "$pv" ] || continue
    disk="/dev/$(lsblk -no PKNAME "$pv" 2>/dev/null | head -1)"
    partnum=$(cat "/sys/class/block/$(basename "$pv")/partition" 2>/dev/null || true)
    if command -v growpart >/dev/null 2>&1 && [ -b "$disk" ] && [ -n "$partnum" ]; then
      # growpart exits non-zero on "NOCHANGE" (already at disk end) — that's fine.
      if growpart "$disk" "$partnum" >/dev/null 2>&1; then
        say "grew partition $pv to fill $disk"
      fi
    fi
    pvresize "$pv" >/dev/null 2>&1 || true
  done < <(pvs --noheadings -o pv_name,vg_name 2>/dev/null | awk -v v="$vg" '$2==v{print $1}')

  # Extend the LV into all free VG space and resize the filesystem (-r handles
  # ext4 and xfs). Only if there's actually free space, so this is a clean no-op
  # on an already-full VG.
  free=$(vgs --noheadings -o vg_free --units b --nosuffix "$vg" 2>/dev/null | tr -dc '0-9')
  if [ "${free:-0}" -gt 1048576 ]; then    # >1 MiB free → worth extending
    if lvextend -r -l +100%FREE "/dev/$vg/$lv" >/dev/null 2>&1; then
      say "extended /dev/$vg/$lv into free VG space"
    else
      say "WARN: lvextend failed (left unchanged) — grow /dev/$vg/$lv manually"
    fi
  fi
  say "root volume now: $(df -h / | awk 'NR==2{print $2" total, "$4" free"}')"
}
grow_root || say "root grow had a problem (non-fatal)"

# 5) Weekly DB VACUUM — return freed pages to the OS ------------------------
# Retention DELETEs rows but SQLite never shrinks the file on its own, so a box
# can sit on a giant mostly-empty DB even while pruning works (observed: a 15 GB
# file holding only ~196k live rows). A weekly VACUUM rebuilds the file to its
# live size. Guarded: only runs when there's real bloat to reclaim AND room for
# the rebuild, finds the worker's actual DB wherever it lives, and uses the venv
# python (no sqlite3 CLI needed). VACUUM briefly write-locks the DB; a busy
# timeout lets it wait for a gap rather than fighting the live worker.
VENV_PY=/opt/gencall/venv/bin/python3
[ -x "$VENV_PY" ] || VENV_PY=$(command -v python3 || true)
if [ -n "$VENV_PY" ]; then
  cat > /etc/cron.weekly/gencall-vacuum <<EOF
#!/bin/sh
# GenCall: weekly VACUUM so pruned-but-not-shrunk DBs return space to disk.
$VENV_PY - <<'PY'
import sqlite3, os, glob, shutil
MIN_BLOAT = 200 * 1024 * 1024   # only bother if >200 MB is reclaimable
for db in sorted(set(glob.glob('/opt/gencall/**/*.db', recursive=True))):
    try:
        size = os.path.getsize(db)
        free = shutil.disk_usage(os.path.dirname(db)).free
        c = sqlite3.connect(db, timeout=120)
        pc = c.execute('PRAGMA freelist_count').fetchone()[0]
        ps = c.execute('PRAGMA page_size').fetchone()[0]
        reclaimable = pc * ps
        # Need free space for the rebuilt copy (~live size = size - reclaimable).
        if reclaimable > MIN_BLOAT and free > (size - reclaimable) + 64 * 1024 * 1024:
            c.execute('VACUUM')
            print('vacuumed', db, 'reclaimed ~%d MB' % (reclaimable // 1048576))
        c.close()
    except Exception as e:
        print('skip', db, e)
PY
EOF
  chmod 0755 /etc/cron.weekly/gencall-vacuum
  say "installed /etc/cron.weekly/gencall-vacuum"
fi

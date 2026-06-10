#!/usr/bin/env bash
#
# GenCall v2 — real-SIPp smoke test. Run ON the Ubuntu box AFTER ./deploy/install.sh.
#
#     ./deploy/smoke-loopback.sh
#
# Why this exists: the 152 automated tests run against a STUB sipp, so they cannot
# catch a real-SIPp scenario-load abort (the kind of bug that was found and fixed
# in loop_uas.xml). This script uses the REAL sipp binary inside the worker image
# to prove the two riskiest things before you trust the box with live traffic:
#
#   PART A (automatic): both loop scenarios LOAD on the real sipp without aborting
#                       — this is the high-value check; it catches bad keywords,
#                       malformed XML, and the [field2]/[senderip]/-key issues.
#   PART B (guided):    a full loopback call through GenCall's own API, so you see
#                       call_records (out + in) and the matcher close the loop.
#
# Exit 0 = Part A passed. Part B prints copy-paste commands (it needs a one-line
# config change + worker restart, so it is guided rather than automatic).
#
set -euo pipefail

COMPOSE="docker compose -f docker-compose.v2.yml"
RTP_LO="${RTP_LO:-16384}"
RTP_HI="${RTP_HI:-16584}"

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[1;32m✓\033[0m %s\n' "$*"; }
bad()  { printf '   \033[1;31m✗ %s\033[0m\n' "$*"; }
warn() { printf '   \033[1;33m!\033[0m %s\n' "$*"; }

$COMPOSE ps >/dev/null 2>&1 || { echo "Stack not up — run ./deploy/install.sh first."; exit 1; }

# ── PART A: do the real scenarios load on real sipp? ──────────────────────────
say "PART A — SIPp present + both loop scenarios load on the real binary"

$COMPOSE exec -T gencall sipp -v >/dev/null 2>&1 \
  && ok "sipp present: $($COMPOSE exec -T gencall sipp -v 2>/dev/null | head -1)" \
  || { bad "sipp not runnable in the worker image"; exit 1; }

UAS_XML="$($COMPOSE exec -T gencall sh -c 'find / -name loop_uas.xml 2>/dev/null | head -1' | tr -d "\r")"
UAC_XML="$($COMPOSE exec -T gencall sh -c 'find / -name loop_uac.xml 2>/dev/null | head -1' | tr -d "\r")"
[ -n "$UAS_XML" ] && [ -n "$UAC_XML" ] || { bad "could not locate the loop scenario templates in the image"; exit 1; }
ok "templates: $UAS_XML / $UAC_XML"

# A scenario that fails to load makes sipp exit FAST with a parse/keyword error.
# A scenario that loads OK sits waiting and is killed by 'timeout' (rc 124). So:
# rc 124  -> loaded fine;  any error text -> load failure.
load_check() {  # load_check <label> <sipp args...>
  local label="$1"; shift
  local out rc
  out="$($COMPOSE exec -T gencall sh -c "timeout 4 sipp $* 2>&1; echo RC=\$?" || true)"
  rc="$(printf '%s' "$out" | sed -n 's/.*RC=\([0-9]*\)$/\1/p' | tail -1)"
  if printf '%s' "$out" | grep -qiE 'unknown (keyword|command)|syntax error|aborting|unable to load|error opening|bad (scenario|message)'; then
    bad "$label scenario FAILED to load on real sipp:"
    printf '%s\n' "$out" | grep -iE 'unknown|syntax|abort|error|bad' | head -5 | sed 's/^/        /'
    return 1
  fi
  # rc 124 = killed by timeout while running = loaded OK.
  ok "$label scenario loads on real sipp (rc=${rc:-?})"
  return 0
}

# Minimal -inf with the 3 columns the UAC expects: a;b;hold_ms (first line = mode).
$COMPOSE exec -T gencall sh -c 'printf "SEQUENTIAL\n1000;2000;2000\n" > /tmp/smoke_pairs.inf'

A_OK=0
load_check "UAS (answer)" \
  "-sf $UAS_XML -i 127.0.0.1 -p 5098 -mi 127.0.0.1 -min_rtp_port $RTP_LO -max_rtp_port $RTP_HI -rtp_echo -key duration_max_s 60 -m 1" || A_OK=1
load_check "UAC (originate)" \
  "127.0.0.1:5098 -sf $UAC_XML -inf /tmp/smoke_pairs.inf -i 127.0.0.1 -p 5097 -min_rtp_port $((RTP_LO+2)) -max_rtp_port $RTP_HI -m 1 -l 1 -r 1" || A_OK=1

if [ "$A_OK" -ne 0 ]; then
  say "RESULT: Part A FAILED — a scenario does not load on real sipp. Do NOT run live traffic."
  echo "   Paste the error lines above back to me and I'll fix the template."
  exit 1
fi
say "RESULT: Part A PASSED — both scenarios are valid on the real sipp binary."

# ── PART B: full loopback through GenCall's API (guided) ──────────────────────
say "PART B — full call through GenCall's own pipeline (guided, ~2 min)"
cat <<EOF
   This places a real call from GenCall's UAC to its own UAS and shows the
   call_records + matcher. Because GenCall blocks loopback destinations by
   default (SSRF guard), it needs a ONE-LINE config change + worker restart:

   1) Allow the box to call itself, then restart the worker:
        printf '\n[loops]\ndest_allowlist = 127.0.0.1\n' >> gencall/etc/gencall.cfg
        # also trust the self-call's source so it isn't flagged:
        #   add 127.0.0.1 to [trust] whitelist in gencall/etc/gencall.cfg
        $COMPOSE restart gencall

   2) Grab your API key (shown once at first boot):
        KEY=\$($COMPOSE logs gencall | sed -n 's/.*X-API-Key: *//p' | head -1)

   3) Start a tiny loopback campaign (2 calls, 3s each) — note the campaign id:
        curl -fsS -X POST http://127.0.0.1:8080/api/loops \\
          -H "X-API-Key: \$KEY" -H 'Content-Type: application/json' \\
          -d '{"name":"smoke","dest_host":"127.0.0.1","dest_port":5060,
               "rate":1,"max_concurrent":1,"duration_s":3,"target_calls":2}'

   4) After ~15s, read the result — expect answered calls and (once the matcher
      runs) loop_stats with minutes_out_ms / minutes_in_ms and a completion %:
        curl -fsS http://127.0.0.1:8080/api/loops \\
          -H "X-API-Key: \$KEY" | python3 -m json.tool

   5) Revert the loopback exception when done (production must NOT allow 127.0.0.1):
        # remove the dest_allowlist line you added, then:
        $COMPOSE restart gencall

   PASS = step 4 shows answered calls AND call_records for BOTH directions
   (an 'out' A-side row and an 'in' B-side row) with sane millisecond durations.
EOF
ok "Part A automated checks complete; follow Part B to prove the full loop."

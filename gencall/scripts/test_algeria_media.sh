#!/usr/bin/env bash
#
# test_algeria_media.sh — fire 3 test calls (A=Nigeria-Lagos, B=Algeria) at the
# switch WITH real PCMA media, capture SIP (+RTP), and report the release cause.
#
# Purpose: isolate the cause-47 ("CAU-RENAU") drops. The failing loop toward
# 208.87.169.100 answers (200 OK) then the far end BYEs ~200ms later with Q.850
# cause=47, with NO media on our side (loops run signaling-only by default). This
# sends the SAME number format that was routing + answering, but now streams a
# looped G.711 A-law tone (rtp_stream, PT 8 PCMA) so the answered calls have real
# audio. If they now hold and clear with cause 16 (normal), missing media was the
# cause. If they STILL die cause 47 with media flowing, it's the switch route
# function (switch-side), not us.
#
# Run ON THE LINUX WORKER (sipp + the gencall repo present). Usage:
#   LOCAL_IP=10.35.21.3 sudo -E ./gencall/scripts/test_algeria_media.sh
#
set -euo pipefail

# ── config (override via env) ────────────────────────────────────────────────
SWITCH="${SWITCH:-208.87.169.100}"      # stay on 169.100 (per decision)
SBC_MEDIA="${SBC_MEDIA:-208.87.169.179}" # SBC media IP seen anchoring RTP (for capture)
SIPP="${SIPP:-sipp}"
HOLD_S="${HOLD_S:-15}"                   # call hold seconds — long enough to prove media sustains
RATE="${RATE:-1}"                        # calls/sec
CAPTURE="${CAPTURE:-1}"                  # 1 = run tcpdump alongside
# local SIP + media (RTP) source IP that reaches the switch; auto-detect if unset
LOCAL_IP="${LOCAL_IP:-$(ip -4 route get "$SWITCH" 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p' | head -1)}"
LOCAL_IP="${LOCAL_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/../scenarios/templates/loop_uac.xml"
WORK="$(mktemp -d /tmp/algeria_test.XXXXXX)"
NUMBERS="$WORK/numbers.csv"
SCEN="$WORK/uac_rtp.xml"
PCAP="$WORK/algeria_media_test.pcap"

# ── 3 A/B pairs ──────────────────────────────────────────────────────────────
# A = Nigeria-Lagos (2341 — the origin prefix your switch FUNCTION matches on).
# DEFAULT below is the 13-digit-A / 12-digit-B format that was ALREADY routing and
# getting answered on 169.100, so this run changes ONLY the media — the cleanest
# test of "does media stop the cause-47 drop?".
#
# Your loops now generate 11-digit Nigeria-Lagos A + 12-digit Algeria B
# (gen_loop_csv kept at valid E.164). Once media is confirmed, re-run with the
# block below to prove that format still matches the function and routes:
#   SEQUENTIAL
#   23410433218;213696001338
#   23418386379;213602654235
#   23411615594;213678161849
cat > "$NUMBERS" <<'CSV'
SEQUENTIAL
2341043321819;213600133890
2341863794026;213642351161
2341594078161;213695931034
CSV

# ── locate (or build) the looped PCMA media sample ───────────────────────────
AUDIO="${AUDIO:-}"
if [[ -z "$AUDIO" ]]; then
  AUDIO="$(find /opt/gencall "$SCRIPT_DIR/.." -name g711a.raw 2>/dev/null | head -1 || true)"
fi
if [[ -z "$AUDIO" || ! -f "$AUDIO" ]]; then
  AUDIO="$WORK/g711a.raw"
  echo "[*] g711a.raw not found — generating a 1s 400Hz A-law tone -> $AUDIO"
  python3 - "$AUDIO" <<'PY'
import sys, math
# 1s of 400Hz sine, 8kHz, encoded to G.711 A-law (PT 8). Headerless raw bytes.
def alaw(s):
    s=max(-32768,min(32767,int(s)));sign=0x80 if s>=0 else 0x00
    if s<0:s=-s-1 if s>-32768 else 32767
    s>>=4
    if s>0xFFF:s=0xFFF
    if s>=0x800:exp=7
    else:
        exp=0;t=s>>4
        while t and exp<7:exp+=1;t>>=1
    mant=(s>>(exp+3))&0x0F if exp else (s>>4)&0x0F
    return (sign|(exp<<4)|mant)^0x55
buf=bytearray()
for n in range(8000):
    buf.append(alaw(2000*math.sin(2*math.pi*400*n/8000)))
open(sys.argv[1],'wb').write(bytes(buf))
PY
fi

# ── render the proven UAC scenario with rtp_stream media injected ────────────
[[ -f "$TEMPLATE" ]] || { echo "ERROR: loop_uac.xml not found at $TEMPLATE"; exit 1; }
python3 - "$TEMPLATE" "$SCEN" "$AUDIO" <<'PY'
import sys
src,out,audio=sys.argv[1:4]
xml=open(src,encoding='utf-8').read()
# -1 = loop the sample for the whole call (continuous energy); PT 8 = PCMA.
nop=f'<nop><action><exec rtp_stream="{audio},-1,8,PCMA/8000" /></action></nop>'
if '<!-- RTP_HOOK -->' not in xml:
    sys.exit("ERROR: RTP_HOOK marker missing in loop_uac.xml")
open(out,'w',encoding='utf-8',newline='').write(xml.replace('<!-- RTP_HOOK -->',nop))
PY

echo "============================================================"
echo " switch     : $SWITCH:5060   (media-anchor seen: $SBC_MEDIA)"
echo " local IP   : $LOCAL_IP   (SIP -i and media -mi)"
echo " audio      : $AUDIO"
echo " numbers    : 3 pairs (A=Nigeria-Lagos 2341 / B=Algeria 213)"
echo " hold       : ${HOLD_S}s   rate: ${RATE}/s"
echo " capture    : $([[ $CAPTURE == 1 ]] && echo "$PCAP" || echo off)"
echo "============================================================"
[[ -n "$LOCAL_IP" ]] || { echo "ERROR: could not determine LOCAL_IP — set LOCAL_IP=<box ip>"; exit 1; }

# ── optional capture ─────────────────────────────────────────────────────────
TCPDUMP_PID=""
if [[ "$CAPTURE" == 1 ]]; then
  if command -v tcpdump >/dev/null 2>&1; then
    tcpdump -i any -w "$PCAP" "host $SWITCH or host $SBC_MEDIA" >/dev/null 2>&1 &
    TCPDUMP_PID=$!
    sleep 1
  else
    echo "[!] tcpdump not found — skipping capture (capture separately with sngrep)"
  fi
fi

# ── fire 3 calls ─────────────────────────────────────────────────────────────
set +e
"$SIPP" "$SWITCH:5060" \
  -sf "$SCEN" -inf "$NUMBERS" -inf_index 0 \
  -m 3 -l 3 -r "$RATE" -d "$((HOLD_S*1000))" \
  -i "$LOCAL_IP" -mi "$LOCAL_IP" \
  -trace_err -error_file "$WORK/sipp_err.log" \
  -trace_screen -screen_file "$WORK/sipp_screen.log" \
  -trace_msg -message_file "$WORK/sipp_msg.log" \
  -timeout 45s -nostdin
RC=$?
set -e

[[ -n "$TCPDUMP_PID" ]] && { sleep 2; kill "$TCPDUMP_PID" 2>/dev/null || true; }

echo
echo "===== SIPp result (rc=$RC) ====="
[[ -f "$WORK/sipp_screen.log" ]] && tail -n 25 "$WORK/sipp_screen.log"

# ── show the release cause from the capture (the whole point) ────────────────
if [[ "$CAPTURE" == 1 && -f "$PCAP" ]] && command -v tshark >/dev/null 2>&1; then
  echo
  echo "===== BYE / final-response causes on the wire ====="
  tshark -r "$PCAP" -Y 'sip.Method=="BYE" || sip.Status-Code>=400' \
    -T fields -e ip.src -e ip.dst -e sip.Method -e sip.Status-Code -e sip.Reason 2>/dev/null \
    | sort | uniq -c
fi

echo
echo "artifacts in: $WORK"
echo "  pcap   : $PCAP   (open in Wireshark / sngrep -I)"
echo "  logs   : sipp_screen.log, sipp_msg.log, sipp_err.log"
echo
echo "VERDICT: calls that HOLD ${HOLD_S}s and clear cause 16 = media fixed it."
echo "         calls still dying ~200ms with cause 47 (with RTP flowing) = switch route function."

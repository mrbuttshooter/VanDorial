#!/usr/bin/env bash
#
# test_algeria_media.sh — fire 3 test calls (A=Nigeria-Lagos, B=Algeria) at the
# switch WITH real PCMA media, capture SIP, and report the release cause.
#
# SELF-CONTAINED: the SIPp scenario, the 3 number pairs and the G.711 A-law media
# sample are all embedded/auto-generated, so this runs from anywhere (e.g. /tmp)
# with no gencall repo present. Needs only: sipp, python3, and tcpdump (capture).
#
# Purpose: isolate the cause-47 ("CAU-RENAU") drops. The failing loop toward
# 208.87.169.100 answers (200 OK) then the far end BYEs ~200ms later with Q.850
# cause=47, with NO media on our side (loops run signaling-only by default). This
# sends the SAME number format that was routing + answering, but now streams a
# looped G.711 A-law tone (rtp_stream, PT 8 PCMA). If the calls now HOLD and clear
# with cause 16 (normal), missing media was the cause. If they STILL die cause 47
# with media flowing, it is the switch route function (switch-side), not us.
#
# Run ON THE LINUX WORKER:
#   sudo -E bash test_algeria_media.sh          # or: chmod +x … ; sudo -E ./test_algeria_media.sh
#
set -euo pipefail

# ── config (override via env) ────────────────────────────────────────────────
SWITCH="${SWITCH:-208.87.169.100}"       # stay on 169.100 (per decision)
SBC_MEDIA="${SBC_MEDIA:-208.87.169.179}" # SBC media IP seen anchoring RTP (for capture)
SIPP="${SIPP:-sipp}"
HOLD_S="${HOLD_S:-15}"                    # call hold seconds — long enough to prove media sustains
RATE="${RATE:-1}"                         # calls/sec
CAPTURE="${CAPTURE:-1}"                   # 1 = run tcpdump alongside
# local SIP + media (RTP) source IP that reaches the switch; auto-detect if unset
LOCAL_IP="${LOCAL_IP:-$(ip -4 route get "$SWITCH" 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p' | head -1)}"
LOCAL_IP="${LOCAL_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"

command -v "$SIPP" >/dev/null 2>&1 || { echo "ERROR: '$SIPP' not found — set SIPP=/path/to/sipp"; exit 1; }
WORK="$(mktemp -d /tmp/algeria_test.XXXXXX)"
NUMBERS="$WORK/numbers.csv"
SCEN="$WORK/uac_rtp.xml"
PCAP="$WORK/algeria_media_test.pcap"

# ── 3 A/B pairs ──────────────────────────────────────────────────────────────
# Numbers MUST match the switch route function TO_DORY_UK:
#   oad (A) = "^....3536.*"  -> A must start 3536  (Irish origin — what Sigma uses)
#   dad (B) = "^..2136.*"    -> B must start 2136  (Algeria mobile)
# Earlier A=2341 (Nigeria-Lagos) did NOT match oad=3536, so calls fell through to
# a default route and dropped (cause 47/31). These mirror Sigma's working format:
# A=3536+8 (12-digit), B=2136+7 (11-digit). Regenerate with:
#   gen_loop_csv --oad-code 3536 --dad-zone "Algeria-Mobile" --dad-length 11
cat > "$NUMBERS" <<'CSV'
SEQUENTIAL
353604332181;21360013389
353683863794;21362654235
353616155940;21368161849
CSV

# ── locate (or build) the looped PCMA media sample ───────────────────────────
AUDIO="${AUDIO:-$(find /opt -name g711a.raw 2>/dev/null | head -1 || true)}"
if [[ -z "$AUDIO" || ! -f "$AUDIO" ]]; then
  AUDIO="$WORK/g711a.raw"
  echo "[*] g711a.raw not found — generating a 1s 400Hz A-law tone -> $AUDIO"
  python3 - "$AUDIO" <<'PY'
import sys, math
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

# ── embedded UAC scenario (the proven Dory INVITE, 8 18 101, + rtp_stream) ───
cat > "$SCEN" <<'XML'
<?xml version="1.0" encoding="ISO-8859-1" ?>
<!DOCTYPE scenario SYSTEM "sipp.dtd">
<scenario name="Algeria media test UAC">
  <send retrans="500">
    <![CDATA[
      INVITE sip:[field1]@[remote_ip] SIP/2.0
      Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=[branch]
      From: "[field0]" <sip:[field0]@[local_ip]>;tag=[pid]SIPpTag00[call_number]
      To: <sip:[field1]@[remote_ip]>
      Call-ID: [call_id]
      CSeq: 102 INVITE
      User-Agent: teles
      Contact: <sip:[field0]@[local_ip]:[local_port]>
      Expires: 15
      Allow: CANCEL,BYE,INVITE, ACK
      Content-Type: application/sdp
      Content-Length: [len]
      Accept: application/sdp

      v=0
      o=Dory 53655765 2353687637 IN IP[local_ip_type] [local_ip]
      s=SIP Call
      c=IN IP[media_ip_type] [media_ip]
      t=0 0
      m=audio [auto_media_port] RTP/AVP 8 18 101
      a=rtpmap:18 G729/8000
      a=fmtp:18 annexb=yes
      a=rtpmap:8 PCMA/8000
      a=rtpmap:101 telephone-event/8000
      a=fmtp:101 0-15
    ]]>
  </send>

  <recv response="100" optional="true" />
  <recv response="180" optional="true" />
  <recv response="183" optional="true" />

  <!-- non-2xx finals: jump to label 40 (ACK + end) so a 404/486/… doesn't hang -->
  <recv response="400" optional="true" next="40" />
  <recv response="403" optional="true" next="40" />
  <recv response="404" optional="true" next="40" />
  <recv response="408" optional="true" next="40" />
  <recv response="480" optional="true" next="40" />
  <recv response="484" optional="true" next="40" />
  <recv response="486" optional="true" next="40" />
  <recv response="487" optional="true" next="40" />
  <recv response="488" optional="true" next="40" />
  <recv response="500" optional="true" next="40" />
  <recv response="503" optional="true" next="40" />
  <recv response="603" optional="true" next="40" />

  <recv response="200" rtd="true" crlf="true" />

  <send>
    <![CDATA[
      ACK sip:[field1]@[remote_ip] SIP/2.0
      Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=[branch]
      From: "[field0]" <sip:[field0]@[local_ip]>;tag=[pid]SIPpTag00[call_number]
      To: <sip:[field1]@[remote_ip]>[peer_tag_param]
      Call-ID: [call_id]
      CSeq: 102 ACK
      Contact: <sip:[field0]@[local_ip]:[local_port]>
      Content-Length: 0
    ]]>
  </send>

  <!-- stream the looped A-law tone (PT 8 PCMA) for the whole call -->
  <nop><action><exec rtp_stream="__AUDIO__,-1,8,PCMA/8000" /></action></nop>

  <pause />

  <send retrans="500">
    <![CDATA[
      BYE sip:[field1]@[remote_ip] SIP/2.0
      Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=[branch]
      From: "[field0]" <sip:[field0]@[local_ip]>;tag=[pid]SIPpTag00[call_number]
      To: <sip:[field1]@[remote_ip]>[peer_tag_param]
      Call-ID: [call_id]
      CSeq: 103 BYE
      Content-Length: 0
    ]]>
  </send>
  <recv response="200" crlf="true" next="1" />

  <!-- failure-ACK for non-2xx finals -->
  <label id="40" />
  <send>
    <![CDATA[
      ACK sip:[field1]@[remote_ip] SIP/2.0
      Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=[branch]
      From: "[field0]" <sip:[field0]@[local_ip]>;tag=[pid]SIPpTag00[call_number]
      To: <sip:[field1]@[remote_ip]>[peer_tag_param]
      Call-ID: [call_id]
      CSeq: 102 ACK
      Content-Length: 0
    ]]>
  </send>
  <label id="1" />
</scenario>
XML
sed -i "s|__AUDIO__|$AUDIO|" "$SCEN"

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
  -sf "$SCEN" -inf "$NUMBERS" \
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

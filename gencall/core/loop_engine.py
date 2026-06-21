"""
LoopEngine (design §4.1 / §4.4 / §5).

Owns the two SIPp roles that make up a minutes-for-minutes loop:

  * ONE persistent **UAS** (answer side) — a single long-lived SIPp bound to the
    SIP port that answers calls returning from MADA with two-way media
    (``-rtp_echo``) and a scenario-level max-duration guard. Started on boot
    (``start_answer()``), monitored, and restarted with a throttled backoff if it
    dies (design §8 — if the UAS dies, answering stops).

  * N **UAC** processes — one per running Loop Campaign. Each originates traffic
    from a number-pair CSV at the campaign's rate/concurrency, holding each call
    for a fixed or uniform-random duration, until stopped or a target (calls or
    minutes) is reached.

Both roles are built from the shipped scenario templates (``loop_uac.xml`` /
``loop_uas.xml``) and launched through the existing ``SIPpEngine`` process
control, so every spawned PID is recorded in the managed-process registry (design
§4.5) for crash-orphan reconciliation — the LoopEngine adds no separate process
plumbing.

Caps (config, defaults for the 2-core/4 GB worker, design §4.1):

  * ``loops_max_concurrent``          max concurrently running campaigns (50),
  * ``loops_max_channels``            per-campaign UAC concurrent cap (1000),
  * ``loops_max_answered``            max simultaneously-answered inbound calls (1100),
  * ``loops_answered_max_duration_s`` per-answered-call ceiling (7200 s).

Campaign rows live in the ``loop_campaigns`` table (migration 0002). Status
moves running → stopped|completed (and is set ``interrupted`` by startup
reconciliation when a crash leaves a campaign's UAC orphaned).

This is control-plane only: the calls and media live in native SIPp. The single
monitor thread sleeps ≥ 1 s between passes — no busy loops (per this codebase's
standard).
"""

import datetime
import logging
import os
import random
import socket
import tempfile
import threading
import uuid

from gencall.core.config import Config
from gencall.core.sipp_engine import (
    SIPpEngine,
    SIPpInstance,
    SIPpMode,
    SIPpState,
    SIPpTransport,
)

logger = logging.getLogger("gencall.loop_engine")

# Built-in loop scenario templates (shipped in gencall/scenarios/templates).
_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scenarios", "templates"
)
UAC_TEMPLATE = os.path.join(_TEMPLATE_DIR, "loop_uac.xml")
UAS_TEMPLATE = os.path.join(_TEMPLATE_DIR, "loop_uas.xml")

# Marker in loop_uac.xml (inside the 200-answer <recv> action) where the RTP
# media action is injected when a campaign has RTP enabled. Replaced with a
# play_pcap_audio exec; left as a harmless comment for signaling-only loops.
_RTP_HOOK = "<!-- RTP_HOOK -->"

def _detect_primary_ip() -> str:
    """Best-effort primary (default-route) IPv4 of this host, or "" if unknown.

    Used as the UAS ``-i``/``-mi`` when ``[sip] local_ip`` is unset, so the answer
    side never advertises ``127.0.0.1`` in its SDP. A loopback media address is a
    silent one-way-audio trap: the switch loops the call back to the UAS, reads
    ``c=IN IP4 127.0.0.1``, has nowhere to send return media, and tears the call
    down (Q.850 cause 47/127). Opens an unconnected UDP socket toward a public
    address (sends NO packets) and reads the local end the kernel would route
    through. Override with ``[sip] local_ip`` on a multi-homed box.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 9))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


# The single UAS instance always carries this fixed engine-internal id so the
# answer side is addressable without a campaign.
UAS_INSTANCE_ID = "loop-uas"

# Monitor poll interval — the answer-side health/restart loop. Floored well above
# the spec's 1 s minimum; the UAS is long-lived so a slow poll is plenty and
# keeps the control plane near-idle (design §4.1 / risks §8).
MONITOR_INTERVAL_S = 2.0

# Shaper wake interval (Phase 2 diurnal shaping). The shaper thread wakes once a
# minute and only relaunches a campaign when the current hour's curve rate
# actually differs from the running rate — so in practice it steps at most once
# per hour. A 60 s wake (not per-second) keeps the control plane near-idle; the
# wait is event-driven (woken early only on stop), never a busy loop.
SHAPER_INTERVAL_S = 60.0

_TRANSPORT_MAP = {
    "udp": SIPpTransport.UDP,
    "tcp": SIPpTransport.TCP,
    "tls": SIPpTransport.TLS,
}


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _new_campaign_id():
    return "loop-" + uuid.uuid4().hex[:12]


class CapExceeded(Exception):
    """Raised when a start would breach a configured cap (design §4.1)."""


class IPBusy(Exception):
    """Raised when a source IP already has a running loop (one loop per IP)."""


class LoopEngine:
    """Owns one persistent UAS + N per-campaign UAC processes.

    ``sipp_engine`` is the shared ``SIPpEngine`` (already wired to the process
    registry, so PID tracking is automatic). ``db`` is a ``Database`` (or None to
    run without persistence — campaign rows are then in-memory only and lost on
    restart; tests usually pass a real sqlite Database).
    """

    def __init__(self, sipp_engine: SIPpEngine, db=None, config: Config = None):
        self.engine = sipp_engine
        self.db = db
        self.config = config or Config()
        # Optional LoopMatcher (design §4.3), wired in main.py. When set, the
        # engine tracks/untracks each campaign on start/stop so the matcher only
        # joins records for running campaigns. None => no loop accounting.
        self.matcher = None
        # Optional CallRecordParser (design §4.2), wired in main.py. When set,
        # the engine registers each UAC/UAS instance's per-call <log> path with
        # the parser on start and removes it on stop, so the parser actually
        # ingests the records the loop produces (otherwise call_records stays
        # empty and every minutes/completion stat is permanently 0).
        self.parser = None
        self._lock = threading.RLock()
        # campaign_id -> in-memory campaign dict (mirrors the DB row + the SIPp
        # instance id owning it). The DB is the source of truth across restarts;
        # this is the live working set.
        self._campaigns: dict[str, dict] = {}
        # Answer-side monitor thread machinery.
        self._monitor_stop = threading.Event()
        self._monitor_thread = None
        self._last_uas_start = 0.0
        # Diurnal shaper thread machinery (Phase 2). Lazily started by
        # start_campaign when a profiled campaign launches; event-driven wait so
        # it idles between hourly steps (no busy loop).
        self._shaper_stop = threading.Event()
        self._shaper_thread = None
        # Monotonic counter making each overlap-relaunch UAC id unique even when
        # two steps land in the same second (the id also carries the rate).
        self._step_seq = 0

    # ── Answer side (persistent UAS) ─────────────────────────────────────────

    def start_answer(self):
        """Start the single persistent UAS (idempotent) and its monitor.

        Builds the UAS instance from ``loop_uas.xml`` with ``-rtp_echo`` for
        two-way media and the answered-call max-duration guard rendered from
        config. Safe to call repeatedly: if the UAS is already running this is a
        no-op. Returns True if the UAS is running on return.
        """
        with self._lock:
            # Prominent boundary warning: with no trust whitelist the app layer
            # verifies nothing, so the host firewall is the ONLY thing keeping a
            # 0.0.0.0:5060 UAS from answering the open internet.
            if not self.config.trust_whitelist:
                logger.warning(
                    "UAS answering 0.0.0.0:5060 with empty trust whitelist — "
                    "firewall is the ONLY boundary. Set [trust] whitelist to the "
                    "MADA source IPs/CIDRs so inbound calls are verified."
                )

            existing = self.engine.get_instance(UAS_INSTANCE_ID)
            if existing is not None and existing.state == SIPpState.RUNNING:
                self._ensure_monitor()
                return True

            instance = self._build_uas_instance()
            import time as _time

            self._last_uas_start = _time.time()
            ok = self.engine.start_instance(instance)
            if ok:
                # Register the UAS per-call log so inbound (B-side) records are
                # ingested into call_records (design §4.2). campaign_id=None: the
                # answer side is shared across campaigns; the matcher scopes
                # inbound by its join window, not by a campaign tag.
                self._register_logs(instance, campaign_id=None)
                logger.info("Loop answer side (UAS) started on the SIP port")
            else:
                logger.error(
                    "Loop answer side (UAS) failed to start: %s",
                    instance.error_message,
                )
            self._ensure_monitor()
            return ok

    def _build_uas_instance(self) -> SIPpInstance:
        """Construct the persistent UAS SIPpInstance from the template.

        ``-rtp_echo`` gives two-way media by echoing; ``-l`` caps simultaneously
        answered calls at the configured ceiling. The scenario renders its own
        max-duration guard from ``[duration_max_s]`` — we pass it as a
        ``-key`` so SIPp substitutes it in the recv timeout (design §4.1).

        The UAS binds the SIP-facing address (config ``[sip] local_ip``) so
        SIPpInstance.build_command emits ``-i``/``-mi`` and the RTP port window;
        that keeps ``-rtp_echo`` and the advertised SDP on the same interface and
        inside the firewalled media range.
        """
        max_answered = self.config.loops_max_answered
        max_dur = self.config.loops_answered_max_duration_s
        # The recv timeout in loop_uas.xml is "[duration_max_s]000" (ms): a 0 or
        # unset guard collapses to a 0 ms timeout that fires immediately and BYEs
        # every call the instant it answers. Enforce a positive guard before
        # launch (fall back to the config default if misconfigured to 0).
        if max_dur <= 0:
            logger.warning(
                "loops_answered_max_duration_s=%s is not positive; the UAS recv "
                "guard would BYE every call. Falling back to 7200s.", max_dur,
            )
            max_dur = 7200
        transport = _TRANSPORT_MAP.get(self.config.sipp_transport.lower(), SIPpTransport.UDP)

        # -rtp_echo: two-way media (SIPp echoes RTP on -mp and -mp+2). The
        # answered-call max-duration guard is a LITERAL in loop_uas.xml's recv
        # timeout (SIPp does not substitute -key keywords inside the timeout
        # attribute), so we no longer pass -key duration_max_s here.
        extra = "-rtp_echo"
        # SIP-facing bind for -i/-mi. A pure answer scenario has no remote to
        # auto-detect an egress IP from, so a blank local_ip makes SIPp advertise
        # c=IN IP4 127.0.0.1 in the UAS SDP — the switch then can't return media
        # and every looped call dies cause 47/127 (a day-long one-way-audio trap).
        # Fall back to the host's primary IP so we never advertise loopback.
        uas_ip = self.config.sip_local_ip or _detect_primary_ip()
        if not self.config.sip_local_ip:
            logger.info(
                "[sip] local_ip unset; UAS binds/advertises auto-detected primary "
                "IP %r (set [sip] local_ip explicitly on a multi-homed box).",
                uas_ip or "(detect failed — SIPp default)",
            )
        return SIPpInstance(
            id=UAS_INSTANCE_ID,
            scenario_file=UAS_TEMPLATE,
            remote_host="0.0.0.0",   # UAS does not originate; placeholder target
            remote_port=self.config.web_port,  # unused by a pure answer scenario
            local_ip=uas_ip,  # SIP-facing bind (-i/-mi); never blank -> never 127.0.0.1
            local_port=5060,
            mode=SIPpMode.UAS,
            transport=transport,
            call_rate=1.0,
            max_calls=0,             # answer forever
            call_limit=max_answered,
            # media_port left 0: SIPpEngine assigns a unique RTP base port from
            # the config window so the UAS, every UAC and one-shot tests never
            # collide on -mp (which made SIPp exit 254 "Address already in use").
            extra_args=extra,
        )

    def answer_status(self) -> dict:
        """Report UAS health + current answered-call count (design §4.4)."""
        inst = self.engine.get_instance(UAS_INSTANCE_ID)
        if inst is None:
            return {
                "running": False,
                "state": "absent",
                "current_answered": 0,
                "max_answered": self.config.loops_max_answered,
                "total_answered": 0,
            }
        running = inst.state == SIPpState.RUNNING
        return {
            "running": running,
            "state": inst.state.value,
            "current_answered": inst.stats.current_calls,
            "max_answered": self.config.loops_max_answered,
            "total_answered": inst.stats.total_calls,
            "error_message": inst.error_message,
        }

    # ── Answer-side monitor (throttled restart, design §8) ───────────────────

    def _ensure_monitor(self):
        """Start the answer-side monitor thread if not already running."""
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="loop-uas-monitor"
        )
        self._monitor_thread.start()

    def _monitor_loop(self):
        """Restart the UAS with throttled backoff if it dies (design §8).

        Event-driven sleep: wakes early only on stop(); otherwise idles a full
        (≥ 1 s) interval so the control plane stays near-zero CPU.
        """
        import time as _time

        backoff = max(1, int(self.config.loops_uas_restart_backoff_s))
        while not self._monitor_stop.is_set():
            try:
                # Hold the engine-level monitor lock across the WHOLE
                # read-decide-restart so two passes (or a pass racing
                # start_answer) can never both decide the UAS is dead and both
                # launch a replacement fighting for :5060 (design §8). STARTING
                # and RUNNING both count as "not dead": start_instance sets
                # STARTING synchronously, so a UAS mid-launch is never restarted.
                with self._lock:
                    inst = self.engine.get_instance(UAS_INSTANCE_ID)
                    dead = inst is None or inst.state in (
                        SIPpState.STOPPED,
                        SIPpState.ERROR,
                    )
                    # Only restart once the backoff window since the last start
                    # has elapsed — never busy-restart a crash loop.
                    if dead and (_time.time() - self._last_uas_start) >= backoff:
                        logger.warning(
                            "Loop UAS not running (state=%s); restarting (backoff %ds)",
                            getattr(inst, "state", None), backoff,
                        )
                        # Drop the dead instance so start_instance re-creates it.
                        if inst is not None:
                            self.engine.remove_instance(UAS_INSTANCE_ID)
                        new = self._build_uas_instance()
                        self._last_uas_start = _time.time()
                        if self.engine.start_instance(new):
                            # Re-register the restarted UAS's (new pid → new
                            # filename) per-call log with the parser.
                            self._register_logs(new, campaign_id=None)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("UAS monitor pass failed: %s", e)
            self._monitor_stop.wait(MONITOR_INTERVAL_S)

    def stop_monitor(self, timeout=5.0):
        """Stop the answer-side monitor thread (used on shutdown)."""
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=timeout)
            self._monitor_thread = None

    # ── Loop Campaigns (per-campaign UAC) ────────────────────────────────────

    def start_campaign(
        self,
        *,
        name="",
        dest_host,
        dest_port=5060,
        transport="udp",
        csv_path="",
        rate=1.0,
        max_concurrent=10,
        duration_mode="fixed",
        duration_s=180,
        duration_max_s=0,
        match_key="exact",
        target_calls=0,
        target_minutes=0,
        local_ip="",
        node_id=None,
        rtp=False,
        rtp_loop=False,
        profile_enabled=False,
        profile_preset="diurnal",
        night_floor=0.25,
        ramp_up_start=6,
        plateau_start=9,
        plateau_end=18,
        ramp_down_end=22,
        tz_offset=0,
    ) -> dict:
        """Start a Loop Campaign: spawn one UAC and persist a 'running' row.

        Enforces the concurrent-campaign cap (design §4.1) — an over-limit start
        raises ``CapExceeded``. ``duration_mode`` is 'fixed' (every call holds
        ``duration_s``) or 'range' (uniform random in ``[duration_s,
        duration_max_s]``). The target is by calls (``-m``) or by minutes; 0 for
        both means "until stopped". Returns the campaign dict.
        """
        with self._lock:
            running = self._running_campaign_count()
            cap = self.config.loops_max_concurrent
            if running >= cap:
                raise CapExceeded(
                    f"max concurrent loops reached ({running}/{cap})"
                )

            # Source IP for this loop's outbound UAC ("Node = IP"). An explicit
            # local_ip overrides the config default; "" => the OS picks per route.
            effective_ip = (local_ip or self.config.sip_local_ip or "").strip()
            # One loop per IP: refuse a second running loop on the same source IP
            # (a bound IP can run exactly one origination loop). An empty IP means
            # "OS-routed" and is not exclusive — many such loops may coexist.
            if effective_ip and self._ip_has_running_loop(effective_ip):
                raise IPBusy(
                    f"source IP {effective_ip} already runs a loop "
                    "(one loop per IP)"
                )

            # Per-campaign resource envelope (OOM guard, design §4.1). Enforced
            # here too — not just at the API model — so a direct engine caller
            # (controller dispatch) can't spawn an unbounded UAC. Reject
            # negatives/zero and anything above the configured caps before we
            # spend a process on it.
            if rate is None or float(rate) <= 0:
                raise CapExceeded(f"rate must be > 0 (got {rate})")
            if float(rate) > self.config.loops_max_rate_cps:
                raise CapExceeded(
                    f"rate {rate} exceeds per-campaign cap "
                    f"{self.config.loops_max_rate_cps} cps"
                )
            if max_concurrent is None or int(max_concurrent) <= 0:
                raise CapExceeded(
                    f"max_concurrent must be > 0 (got {max_concurrent})"
                )
            if int(max_concurrent) > self.config.loops_max_channels:
                raise CapExceeded(
                    f"max_concurrent {max_concurrent} exceeds per-campaign "
                    f"channel cap {self.config.loops_max_channels}"
                )
            for label, val in (("duration_s", duration_s),
                               ("duration_max_s", duration_max_s),
                               ("target_calls", target_calls),
                               ("target_minutes", target_minutes)):
                if val is not None and int(val) < 0:
                    raise CapExceeded(f"{label} must be >= 0 (got {val})")

            campaign_id = _new_campaign_id()
            instance_id = f"uac-{campaign_id}"

            # Resolve the per-call hold. loop_uac.xml holds with
            # <pause milliseconds="[field2]"/>, so the hold is carried in the
            # -inf row's THIRD column ([field2], ms) for BOTH fixed and range
            # modes — we never pass -d (an attributed <pause> ignores -d, and
            # -d would also lose the CSV's per-row range). _prepare_csv always
            # returns a CSV whose field2 is the per-call hold in ms.
            resolved_csv = self._prepare_csv(
                csv_path, duration_mode, duration_s, duration_max_s
            )

            # max_calls (-m): the call target. A minute target has no direct SIPp
            # flag — it is enforced by the monitor / stop path, so -m stays 0.
            max_calls = int(target_calls) if target_calls and target_calls > 0 else 0

            # Initial rate. For a profiled (diurnal) campaign, START at the current
            # hour's curve value rather than the request's nominal rate, so the
            # campaign reads as organic from its first minute (the shaper thread
            # then keeps it on the curve). _shaper_target_rate clamps to the cap;
            # if it yields 0 (e.g. no target_minutes) we fall back to the nominal
            # rate so we never launch a 0-cps UAC.
            effective_rate = float(rate)
            if profile_enabled:
                import time as _time
                profile_view = {
                    "duration_s": int(duration_s),
                    "target_minutes": int(target_minutes or 0),
                    "night_floor": float(night_floor),
                    "ramp_up_start": int(ramp_up_start),
                    "plateau_start": int(plateau_start),
                    "plateau_end": int(plateau_end),
                    "ramp_down_end": int(ramp_down_end),
                    "tz_offset": int(tz_offset),
                }
                hour_rate = self._shaper_target_rate(
                    profile_view, _time.localtime().tm_hour)
                if hour_rate > 0:
                    effective_rate = hour_rate

            tr = _TRANSPORT_MAP.get((transport or "udp").lower(), SIPpTransport.UDP)
            instance = SIPpInstance(
                id=instance_id,
                scenario_file=self._uac_scenario(bool(rtp), bool(rtp_loop)),
                remote_host=dest_host,
                remote_port=int(dest_port),
                local_port=0,             # OS-assigned ephemeral source port
                # Source IP for outbound calls (per-loop "Node = IP"). Empty =>
                # the OS picks per routing. Pinned to the chosen server's IP so
                # MADA sees the whitelisted VanDorial origination source.
                local_ip=effective_ip,
                mode=SIPpMode.UAC,
                transport=tr,
                call_rate=effective_rate,
                max_calls=max_calls,
                call_limit=int(max_concurrent),
                # Hold each call for duration_s via SIPp -d (an attributed <pause>
                # cannot read a per-row [field2] — SIPp does not substitute -inf
                # fields inside the milliseconds attribute). The UAC scenario uses
                # a bare <pause/>, which honours -d. (Per-call random range is a
                # follow-up: it needs a SIPp call-variable pause, not -d.)
                duration=int(duration_s) if duration_s else 0,
                # media_port left 0: SIPpEngine assigns a unique RTP base port, so
                # concurrent campaigns' UACs (and the UAS) never bind the same -mp.
                csv_file=resolved_csv,
                campaign_id=campaign_id,
            )

            ok = self.engine.start_instance(instance)
            if not ok:
                raise RuntimeError(
                    instance.error_message or "UAC failed to start"
                )

            # Register the UAC's per-call <log> path so its outbound (A-side)
            # records are ingested into call_records under this campaign (§4.2).
            self._register_logs(instance, campaign_id=campaign_id)

            now = _now_iso()
            campaign = {
                "id": campaign_id,
                "name": name or campaign_id,
                "status": "running",
                "node_id": node_id,
                "local_ip": effective_ip,
                "dest_host": dest_host,
                "dest_port": int(dest_port),
                "transport": transport,
                "csv_path": csv_path,
                # The rate the UAC is actually running at (the current hour's curve
                # value for a profiled campaign; the request's nominal rate
                # otherwise). The shaper steps this hourly.
                "rate": effective_rate,
                "max_concurrent": int(max_concurrent),
                "duration_mode": duration_mode,
                "duration_s": int(duration_s),
                "duration_max_s": int(duration_max_s),
                "match_key": match_key,
                "target_calls": int(target_calls or 0),
                "target_minutes": int(target_minutes or 0),
                "rtp": bool(rtp),
                "rtp_loop": bool(rtp_loop),
                # Diurnal traffic profile (Phase 2 shaper). Read by the shaper
                # thread (Task 7); stored verbatim so a restart carries it.
                "profile_enabled": bool(profile_enabled),
                "profile_preset": profile_preset or "diurnal",
                "night_floor": float(night_floor),
                "ramp_up_start": int(ramp_up_start),
                "plateau_start": int(plateau_start),
                "plateau_end": int(plateau_end),
                "ramp_down_end": int(ramp_down_end),
                "tz_offset": int(tz_offset),
                "created_at": now,
                "started_at": now,
                "stopped_at": None,
                "instance_id": instance_id,
            }
            self._campaigns[campaign_id] = campaign
            self._persist_campaign(campaign)
            # Tell the matcher to start joining this campaign's records (§4.3).
            if self.matcher is not None:
                try:
                    self.matcher.track(campaign_id, match_key)
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning("Could not track campaign %s for matching: %s",
                                   campaign_id, e)
            logger.info(
                "Loop campaign %s started (UAC %s -> %s:%s)",
                campaign_id, instance_id, dest_host, dest_port,
            )
            # A profiled campaign needs the shaper running so its rate tracks the
            # diurnal curve hour by hour. Idempotent + idle-safe (event wait).
            if profile_enabled:
                self._ensure_shaper()
            return self._public_campaign(campaign)

    def stop_campaign(self, campaign_id: str) -> dict:
        """Stop a running campaign: kill its UAC and mark the row 'stopped'.

        Returns the updated campaign dict, or raises ``KeyError`` if unknown.
        """
        with self._lock:
            campaign = self._campaigns.get(campaign_id)
            if campaign is None:
                # May be a row we don't have in memory (post-restart). Look it up.
                campaign = self._load_campaign(campaign_id)
                if campaign is None:
                    raise KeyError(campaign_id)

            instance_id = campaign.get("instance_id") or f"uac-{campaign_id}"
            inst = self.engine.get_instance(instance_id)
            self.engine.stop_instance(instance_id)
            # Drain any final terminal records from this UAC's log, THEN stop
            # tailing it: a last poll captures the closing BYE lines before the
            # log file goes quiet, so the final match pass below sees them.
            if self.parser is not None and inst is not None:
                try:
                    self.parser.poll_once()
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning("Final parse pass for %s failed: %s",
                                   campaign_id, e)
                self._unregister_logs(inst)
            campaign["status"] = "stopped"
            campaign["stopped_at"] = _now_iso()
            self._campaigns[campaign_id] = campaign
            self._update_campaign_status(
                campaign_id, "stopped", stopped_at=campaign["stopped_at"]
            )
            # Run a final match pass then stop tracking (§4.3): a stopped campaign
            # should still get its last loop_stats snapshot before going quiet.
            if self.matcher is not None:
                try:
                    self.matcher.match_campaign(
                        campaign_id, match_key=campaign.get("match_key", "exact")
                    )
                    self.matcher.untrack(campaign_id)
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning("Final match pass for %s failed: %s",
                                   campaign_id, e)
            logger.info("Loop campaign %s stopped", campaign_id)
            return self._public_campaign(campaign)

    def step_campaign_rate(self, campaign_id: str, new_rate: float) -> bool:
        """Change a running campaign's attempt rate with NO traffic dip.

        Overlap relaunch: start a fresh UAC at ``new_rate``, then gracefully drain
        (stop + remove) the old one. ACD/hold, concurrency cap, scenario, dest and
        source IP are carried over from the old instance unchanged — only the rate
        changes. SIPp has no live rate-change for a backgrounded instance, so a
        relaunch is how the rate moves; running the two briefly in parallel means
        the curve has no hourly gap.

        Returns False if the campaign isn't running, the new rate is invalid
        (<= 0 or above the per-campaign cap), the rate is effectively unchanged,
        or the replacement UAC fails to start (the old one is then left running).
        """
        with self._lock:
            campaign = self._campaigns.get(campaign_id)
            if campaign is None or campaign.get("status") != "running":
                return False
            try:
                new_rate = float(new_rate)
            except (TypeError, ValueError):
                return False
            if new_rate <= 0 or new_rate > self.config.loops_max_rate_cps:
                return False
            if abs(new_rate - float(campaign.get("rate", 0))) < 1e-9:
                return False

            old_iid = campaign["instance_id"]
            old = self.engine.get_instance(old_iid)
            if old is None:
                return False

            # Build the replacement UAC from the old instance's settings + the new
            # rate. local_port=0 => the OS assigns a fresh ephemeral SIP source
            # port; SIPpEngine assigns a distinct -mp media port — so even though
            # both UACs briefly share one local_ip there is no port collision.
            self._step_seq += 1
            new_iid = f"uac-{campaign_id}-{int(new_rate * 1000)}-{self._step_seq}"
            new = SIPpInstance(
                id=new_iid,
                scenario_file=old.scenario_file,
                remote_host=old.remote_host,
                remote_port=old.remote_port,
                local_port=0,                 # OS-assigned ephemeral (distinct)
                local_ip=old.local_ip,
                mode=SIPpMode.UAC,
                transport=old.transport,
                call_rate=new_rate,
                max_calls=old.max_calls,
                call_limit=old.call_limit,
                duration=old.duration,        # hold carried over unchanged
                csv_file=old.csv_file,
                campaign_id=campaign_id,
            )
            if not self.engine.start_instance(new):
                logger.warning(
                    "shaper: replacement UAC for %s failed (%s); keeping old at %s cps",
                    campaign_id, new.error_message, campaign.get("rate"),
                )
                return False
            # Register the new UAC's per-call log with the parser (the old one's
            # is unregistered when it is drained, below).
            self._register_logs(new, campaign_id=campaign_id)
            campaign["instance_id"] = new_iid
            campaign["rate"] = new_rate
            self._update_campaign_rate(campaign_id, new_rate)

        # Drain the old UAC OUTSIDE the lock (stop_instance signals + waits up to
        # ~10 s): the new UAC is already placing calls, so the old one's in-flight
        # calls finishing causes no dip. Unregister its log so the parser stops
        # tailing a file that will go quiet.
        try:
            if self.parser is not None:
                self._unregister_logs(old)
            self.engine.stop_instance(old_iid)
            self.engine.remove_instance(old_iid)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("shaper: draining old UAC %s failed: %s", old_iid, e)
        logger.info("Loop campaign %s rate stepped to %s cps (UAC %s -> %s)",
                    campaign_id, new_rate, old_iid, new_iid)
        return True

    # ── Diurnal traffic shaper (Phase 2: hourly step along the curve) ─────────

    def _shaper_target_rate(self, campaign: dict, hour: int) -> float:
        """Per-hour attempt rate for a profiled campaign, clamped to the cap.

        Uses the same pure ``traffic_profile.calculate`` the Calculator uses, so a
        running campaign and the Calculator agree to the digit. ACD == the
        campaign's hold (``duration_s``); the daily target is ``target_minutes``.
        """
        from gencall.core import traffic_profile
        prof = {k: campaign.get(k) for k in (
            "night_floor", "ramp_up_start", "plateau_start",
            "plateau_end", "ramp_down_end", "tz_offset")
            if campaign.get(k) is not None}
        acd = int(campaign.get("duration_s") or 0) or 1
        res = traffic_profile.calculate(
            int(campaign.get("target_minutes") or 0), acd, prof)
        cps = res["per_hour"][hour % 24]["cps"]
        return min(max(cps, 0.0), self.config.loops_max_rate_cps)

    def _ensure_shaper(self):
        """Start the diurnal shaper thread if enabled and not already running."""
        if not self.config.loops_shaper_enabled:
            return
        if self._shaper_thread is not None and self._shaper_thread.is_alive():
            return
        self._shaper_stop.clear()
        self._shaper_thread = threading.Thread(
            target=self._shaper_loop, daemon=True, name="loop-shaper")
        self._shaper_thread.start()

    def _shaper_loop(self):
        """Each wake, step every running profiled campaign to the current hour's
        curve rate (overlap relaunch). Idles ``SHAPER_INTERVAL_S`` between wakes
        on an event wait — woken early only by stop_shaper, never a busy loop."""
        import time as _time
        while not self._shaper_stop.is_set():
            try:
                hour = _time.localtime().tm_hour
                # Snapshot the items so a concurrent start/stop can't mutate the
                # dict mid-iteration.
                for cid, campaign in list(self._campaigns.items()):
                    if (campaign.get("status") != "running"
                            or not campaign.get("profile_enabled")):
                        continue
                    target = self._shaper_target_rate(campaign, hour)
                    if target > 0 and abs(
                            target - float(campaign.get("rate", 0))) > 1e-3:
                        self.step_campaign_rate(cid, target)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("shaper pass failed: %s", e)
            self._shaper_stop.wait(SHAPER_INTERVAL_S)

    def stop_shaper(self, timeout=5.0):
        """Stop the diurnal shaper thread (used on shutdown)."""
        self._shaper_stop.set()
        if self._shaper_thread is not None:
            self._shaper_thread.join(timeout=timeout)
            self._shaper_thread = None

    def list_campaigns(self) -> list:
        """List campaigns (DB-backed if available, else the in-memory set)."""
        rows = self._load_all_campaigns()
        if rows is not None:
            return [self._public_campaign(r) for r in rows]
        with self._lock:
            return [self._public_campaign(c) for c in self._campaigns.values()]

    def get_campaign(self, campaign_id: str) -> dict:
        """Live status for one campaign incl. its UAC's current SIPp stats.

        Raises ``KeyError`` if the campaign is unknown.
        """
        campaign = self._campaigns.get(campaign_id) or self._load_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        public = self._public_campaign(campaign)
        inst = self.engine.get_instance(
            campaign.get("instance_id") or f"uac-{campaign_id}"
        )
        public["sipp"] = inst.to_dict() if inst is not None else None
        return public

    # ── helpers: call-record log registration (design §4.2) ──────────────────

    def _register_logs(self, instance, campaign_id):
        """Register a SIPp instance's per-call <log> path(s) with the parser.

        No-op when no parser is wired. Registers every candidate path the
        instance may write to (the deterministic .calllog the stub uses, plus
        the real-SIPp <scenario>_<pid>_logs.log once the pid is known) under the
        owning ``campaign_id`` (None for the shared UAS answer side). Tracking a
        not-yet-existent path is harmless — the parser skips missing files.
        """
        if self.parser is None or instance is None:
            return
        try:
            for path in instance.log_file_candidates():
                self.parser.add_log_file(path, campaign_id=campaign_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Could not register call-log for %s: %s",
                           getattr(instance, "id", "?"), e)

    def _unregister_logs(self, instance):
        """Stop tracking a SIPp instance's per-call <log> path(s)."""
        if self.parser is None or instance is None:
            return
        try:
            for path in instance.log_file_candidates():
                self.parser.remove_log_file(path)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Could not unregister call-log for %s: %s",
                           getattr(instance, "id", "?"), e)

    # ── helpers: CSV / caps / persistence ────────────────────────────────────

    def _running_campaign_count(self) -> int:
        """Count campaigns whose UAC is currently RUNNING (the real cap basis).

        Uses the live engine state rather than the DB so a campaign whose UAC has
        exited (target reached, crash) no longer counts against the cap.
        """
        count = 0
        for cid, c in self._campaigns.items():
            inst = self.engine.get_instance(c.get("instance_id") or f"uac-{cid}")
            if inst is not None and inst.state == SIPpState.RUNNING:
                count += 1
        return count

    def _ip_has_running_loop(self, ip: str) -> bool:
        """True if some campaign with source IP ``ip`` has a RUNNING UAC.

        In-memory (the check + start hold ``self._lock``, so it is race-free
        within this process). Assumes a SINGLE worker process: a systemd restart
        kills all SIPp children and startup reconciliation marks survivors
        interrupted, so there is no live loop the empty in-memory set would miss.
        Do NOT run multiple uvicorn workers without a DB-backed guard."""
        for cid, c in self._campaigns.items():
            if (c.get("local_ip") or "").strip() != ip:
                continue
            inst = self.engine.get_instance(c.get("instance_id") or f"uac-{cid}")
            if inst is not None and inst.state == SIPpState.RUNNING:
                return True
        return False

    def _uac_scenario(self, rtp: bool, rtp_loop: bool = False) -> str:
        """Return the UAC scenario path for this campaign.

        Signaling-only (rtp=False): the shipped loop_uac.xml as-is. With media
        (rtp=True): a per-campaign copy whose RTP_HOOK is replaced by a single
        ``rtp_stream`` exec (a ``<nop>`` after the ACK) that streams the
        configured raw A-law file over the call's media socket; the -rtp_echo UAS
        echoes it → two-way media. ``rtp_loop`` selects the loop count: -1
        (stream continuously for the whole call) vs 1 (play once). rtp_stream is
        non-blocking, so the bare -d ``<pause>`` still holds the call while the
        media streams in the background.

        rtp_stream replaced play_pcap_audio: pcapplay sent via a RAW socket
        (needed CAP_NET_RAW, else SIGSEGV) and, looped via ~37 unrolled plays,
        tore down ~30% of calls (proven by A/B on cy214). rtp_stream uses the
        normal media socket (no cap) and loops natively, holding calls cleanly.

        Falls back to signaling-only if the audio file is missing or the template
        can't be rendered, so a bad media config never blocks a loop.
        """
        if not rtp:
            return UAC_TEMPLATE
        audio = (self.config.loops_rtp_audio or "").strip()
        if not audio or not os.path.isfile(audio):
            logger.warning(
                "RTP requested but audio file not found (%s); starting loop "
                "WITHOUT media. Set [loops] rtp_audio to a raw A-law file.", audio,
            )
            return UAC_TEMPLATE
        try:
            with open(UAC_TEMPLATE, "r", encoding="utf-8") as fh:
                xml = fh.read()
            if _RTP_HOOK not in xml:
                logger.warning("loop_uac.xml has no RTP_HOOK; media not injected")
                return UAC_TEMPLATE
            from xml.sax.saxutils import quoteattr
            # filename,loopcount,payloadtype,payloadparam — -1 loops for the whole
            # call, 1 plays once; PT 8 = PCMA, matching the SDP offer.
            loop = -1 if rtp_loop else 1
            spec = f"{audio},{loop},8,PCMA/8000"
            media = (f"<nop><action><exec rtp_stream={quoteattr(spec)} />"
                     f"</action></nop>")
            rendered = xml.replace(_RTP_HOOK, media)
            fd, path = tempfile.mkstemp(prefix="gencall_uac_rtp_", suffix=".xml")
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(rendered)
            return path
        except OSError as e:  # pragma: no cover - defensive
            logger.warning("Could not render RTP UAC scenario: %s", e)
            return UAC_TEMPLATE

    def _prepare_csv(self, csv_path, duration_mode, duration_s, duration_max_s):
        """Return a SIPp ``-inf`` path whose THIRD column ([field2]) is the
        per-call hold in milliseconds.

        loop_uac.xml holds each call with ``<pause milliseconds="[field2]"/>``,
        so the hold MUST travel in the CSV row (an attributed <pause> ignores the
        ``-d`` flag). We therefore ALWAYS render a generated -inf file carrying a
        field2 hold, for both modes:

          * 'fixed' (or any non-'range') — every row gets the same hold,
            ``duration_s`` rendered to ms (sub-second precision preserved end to
            end: there is no //1000 then *1000 round-trip).
          * 'range' — each row gets an independent uniform-random hold in ms in
            ``[duration_s*1000, duration_max_s*1000]``.

        When no usable CSV is supplied we synthesize a single A/B pair so the UAC
        still has a number pair to dial.
        """
        pairs = self._read_pairs(csv_path) or [("1000000000", "2000000000")]

        if duration_mode == "range":
            lo = int(duration_s) * 1000 if duration_s else 1000
            hi = int(duration_max_s) * 1000 if duration_max_s else lo
            if hi < lo:
                lo, hi = hi, lo
            rows = [(a, b, random.randint(lo, hi)) for (a, b) in pairs]
        else:
            # Fixed (or none): one shared hold in ms. Guard against a 0/empty
            # duration collapsing the pause to nothing — fall back to 1000 ms.
            fixed_ms = int(duration_s) * 1000 if duration_s else 0
            if fixed_ms <= 0:
                fixed_ms = 1000
            rows = [(a, b, fixed_ms) for (a, b) in pairs]

        return self._write_inf(rows, None)

    @staticmethod
    def _read_pairs(csv_path):
        """Read A/B pairs from a plain CSV (``;`` or ``,`` separated). [] if none."""
        if not csv_path or not os.path.isfile(csv_path):
            return []
        pairs = []
        try:
            with open(csv_path, "r", encoding="utf-8", errors="replace") as fh:
                for idx, line in enumerate(fh):
                    line = line.strip()
                    if not line:
                        continue
                    if idx == 0 and line.upper() in ("SEQUENTIAL", "RANDOM", "USERS"):
                        continue
                    cells = line.split(";") if ";" in line else line.split(",")
                    if len(cells) >= 2:
                        pairs.append((cells[0], cells[1]))
        except OSError:
            pass
        return pairs

    @staticmethod
    def _write_inf(rows, _unused):
        """Write a SIPp ``-inf`` file (``RANDOM`` + ``;``-rows) to a temp path.

        ``RANDOM`` so each call draws a random row from the (large) number pool
        rather than marching the file in order. ``rows`` are 2-tuples (a, b) or
        3-tuples (a, b, duration_ms). Returns the file path. The file lives in
        the platform temp dir; it is a generated artifact for one campaign's UAC.
        """
        fd, path = tempfile.mkstemp(prefix="gencall_loop_", suffix=".csv")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write("RANDOM\n")
                for row in rows:
                    fh.write(";".join(str(c) for c in row) + ";\n")
        except OSError:
            pass
        return path

    def _public_campaign(self, campaign: dict) -> dict:
        """Strip engine-internal keys for API responses."""
        public = {k: v for k, v in campaign.items() if k != "instance_id"}
        return public

    # ── DB persistence (raw SQL, no ORM dependency) ──────────────────────────

    def _persist_campaign(self, campaign: dict):
        if self.db is None:
            return
        try:
            from sqlalchemy import text

            with self.db.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO loop_campaigns "
                        "(id, name, status, node_id, local_ip, dest_host, "
                        " dest_port, transport, csv_path, rate, max_concurrent, "
                        " duration_mode, duration_s, duration_max_s, match_key, "
                        " target_calls, target_minutes, profile_enabled, "
                        " profile_preset, night_floor, ramp_up_start, "
                        " plateau_start, plateau_end, ramp_down_end, tz_offset, "
                        " created_at, started_at, stopped_at) "
                        "VALUES (:id, :name, :status, :node_id, :local_ip, "
                        " :dest_host, :dest_port, :transport, :csv_path, :rate, "
                        " :max_concurrent, :duration_mode, :duration_s, "
                        " :duration_max_s, :match_key, :target_calls, "
                        " :target_minutes, :profile_enabled, :profile_preset, "
                        " :night_floor, :ramp_up_start, :plateau_start, "
                        " :plateau_end, :ramp_down_end, :tz_offset, :created_at, "
                        " :started_at, :stopped_at)"
                    ),
                    {k: campaign.get(k) for k in (
                        "id", "name", "status", "node_id", "local_ip",
                        "dest_host", "dest_port", "transport", "csv_path", "rate",
                        "max_concurrent", "duration_mode", "duration_s",
                        "duration_max_s", "match_key", "target_calls",
                        "target_minutes", "profile_enabled", "profile_preset",
                        "night_floor", "ramp_up_start", "plateau_start",
                        "plateau_end", "ramp_down_end", "tz_offset", "created_at",
                        "started_at", "stopped_at",
                    )},
                )
        except Exception as e:
            logger.warning("Could not persist loop campaign %s: %s",
                           campaign.get("id"), e)

    def _update_campaign_status(self, campaign_id, status, stopped_at=None):
        if self.db is None:
            return
        try:
            from sqlalchemy import text

            with self.db.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE loop_campaigns SET status = :status, "
                        "stopped_at = COALESCE(:stopped_at, stopped_at) "
                        "WHERE id = :id"
                    ),
                    {"status": status, "stopped_at": stopped_at, "id": campaign_id},
                )
        except Exception as e:
            logger.warning("Could not update loop campaign %s status: %s",
                           campaign_id, e)

    def _update_campaign_rate(self, campaign_id, rate):
        """Persist a shaper rate step so a reloaded campaign reflects its current
        (curve-stepped) rate, not the rate it was first started at."""
        if self.db is None:
            return
        try:
            from sqlalchemy import text

            with self.db.engine.begin() as conn:
                conn.execute(
                    text("UPDATE loop_campaigns SET rate = :rate WHERE id = :id"),
                    {"rate": float(rate), "id": campaign_id},
                )
        except Exception as e:
            logger.warning("Could not update loop campaign %s rate: %s",
                           campaign_id, e)

    def _row_to_campaign(self, row):
        keys = (
            "id", "name", "status", "node_id", "local_ip", "dest_host",
            "dest_port", "transport", "csv_path", "rate", "max_concurrent",
            "duration_mode", "duration_s", "duration_max_s", "match_key",
            "target_calls", "target_minutes", "profile_enabled",
            "profile_preset", "night_floor", "ramp_up_start", "plateau_start",
            "plateau_end", "ramp_down_end", "tz_offset", "created_at",
            "started_at", "stopped_at",
        )
        c = dict(zip(keys, row))
        c["instance_id"] = f"uac-{c['id']}"
        # Normalise the profile flag so a reloaded campaign behaves like a live
        # one (sqlite stores BOOLEAN as 0/1; the shaper checks truthiness).
        if "profile_enabled" in c:
            c["profile_enabled"] = bool(c["profile_enabled"])
        return c

    def _load_campaign(self, campaign_id):
        if self.db is None:
            return None
        try:
            from sqlalchemy import text

            with self.db.engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT id, name, status, node_id, local_ip, dest_host, dest_port, "
                        "transport, csv_path, rate, max_concurrent, duration_mode, "
                        "duration_s, duration_max_s, match_key, target_calls, "
                        "target_minutes, profile_enabled, profile_preset, night_floor, "
                        "ramp_up_start, plateau_start, plateau_end, ramp_down_end, "
                        "tz_offset, created_at, started_at, stopped_at "
                        "FROM loop_campaigns WHERE id = :id"
                    ),
                    {"id": campaign_id},
                ).fetchone()
            return self._row_to_campaign(row) if row else None
        except Exception as e:
            logger.warning("Could not load loop campaign %s: %s", campaign_id, e)
            return None

    def _load_all_campaigns(self):
        if self.db is None:
            return None
        try:
            from sqlalchemy import text

            with self.db.engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT id, name, status, node_id, local_ip, dest_host, dest_port, "
                        "transport, csv_path, rate, max_concurrent, duration_mode, "
                        "duration_s, duration_max_s, match_key, target_calls, "
                        "target_minutes, profile_enabled, profile_preset, night_floor, "
                        "ramp_up_start, plateau_start, plateau_end, ramp_down_end, "
                        "tz_offset, created_at, started_at, stopped_at "
                        "FROM loop_campaigns ORDER BY created_at DESC"
                    )
                ).fetchall()
            return [self._row_to_campaign(r) for r in rows]
        except Exception as e:
            logger.warning("Could not list loop campaigns: %s", e)
            return None

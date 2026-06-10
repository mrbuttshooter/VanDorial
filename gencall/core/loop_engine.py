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

Caps (config, defaults for the 4 GB box, design §4.1):

  * ``loops_max_concurrent``          max concurrently running campaigns (50),
  * ``loops_max_answered``            max simultaneously-answered inbound calls (120),
  * ``loops_answered_max_duration_s`` per-answered-call ceiling (7200 s).

Campaign rows live in the ``loop_campaigns`` table (migration 0002). Status
moves running → stopped|completed (and is set ``interrupted`` by startup
reconciliation when a crash leaves a campaign's UAC orphaned).

This is control-plane only: the calls and media live in native SIPp. The single
monitor thread sleeps ≥ 1 s between passes — no busy loops (per this codebase's
standard).
"""

import csv
import datetime
import io
import logging
import os
import random
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

# The single UAS instance always carries this fixed engine-internal id so the
# answer side is addressable without a campaign.
UAS_INSTANCE_ID = "loop-uas"

# Monitor poll interval — the answer-side health/restart loop. Floored well above
# the spec's 1 s minimum; the UAS is long-lived so a slow poll is plenty and
# keeps the control plane near-idle (design §4.1 / risks §8).
MONITOR_INTERVAL_S = 2.0

_TRANSPORT_MAP = {
    "udp": SIPpTransport.UDP,
    "tcp": SIPpTransport.TCP,
    "tls": SIPpTransport.TLS,
}


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat()


def _new_campaign_id():
    return "loop-" + uuid.uuid4().hex[:12]


class CapExceeded(Exception):
    """Raised when a start would breach a configured cap (design §4.1)."""


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

        # -rtp_echo: two-way media; -key duration_max_s: scenario guard value.
        extra = f"-rtp_echo -key duration_max_s {max_dur}"
        return SIPpInstance(
            id=UAS_INSTANCE_ID,
            scenario_file=UAS_TEMPLATE,
            remote_host="0.0.0.0",   # UAS does not originate; placeholder target
            remote_port=self.config.web_port,  # unused by a pure answer scenario
            local_ip=self.config.sip_local_ip,  # SIP-facing bind (-i/-mi); "" = all ifaces
            local_port=5060,
            mode=SIPpMode.UAS,
            transport=transport,
            call_rate=1.0,
            max_calls=0,             # answer forever
            call_limit=max_answered,
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

            tr = _TRANSPORT_MAP.get((transport or "udp").lower(), SIPpTransport.UDP)
            instance = SIPpInstance(
                id=instance_id,
                scenario_file=UAC_TEMPLATE,
                remote_host=dest_host,
                remote_port=int(dest_port),
                local_port=0,             # OS-assigned ephemeral source port
                mode=SIPpMode.UAC,
                transport=tr,
                call_rate=float(rate),
                max_calls=max_calls,
                call_limit=int(max_concurrent),
                duration=0,  # hold is per-row [field2] ms in the CSV, not -d
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
                "node_id": None,
                "dest_host": dest_host,
                "dest_port": int(dest_port),
                "transport": transport,
                "csv_path": csv_path,
                "rate": float(rate),
                "max_concurrent": int(max_concurrent),
                "duration_mode": duration_mode,
                "duration_s": int(duration_s),
                "duration_max_s": int(duration_max_s),
                "match_key": match_key,
                "target_calls": int(target_calls or 0),
                "target_minutes": int(target_minutes or 0),
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
        """Write a SIPp ``-inf`` file (``SEQUENTIAL`` + ``;``-rows) to a temp path.

        ``rows`` are 2-tuples (a, b) or 3-tuples (a, b, duration_ms). Returns the
        file path. The file lives in the platform temp dir; it is a generated
        artifact for one campaign's UAC.
        """
        fd, path = tempfile.mkstemp(prefix="gencall_loop_", suffix=".csv")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write("SEQUENTIAL\n")
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
                        "(id, name, status, node_id, dest_host, dest_port, "
                        " transport, csv_path, rate, max_concurrent, "
                        " duration_mode, duration_s, duration_max_s, match_key, "
                        " target_calls, target_minutes, created_at, started_at, "
                        " stopped_at) "
                        "VALUES (:id, :name, :status, :node_id, :dest_host, "
                        " :dest_port, :transport, :csv_path, :rate, "
                        " :max_concurrent, :duration_mode, :duration_s, "
                        " :duration_max_s, :match_key, :target_calls, "
                        " :target_minutes, :created_at, :started_at, :stopped_at)"
                    ),
                    {k: campaign.get(k) for k in (
                        "id", "name", "status", "node_id", "dest_host",
                        "dest_port", "transport", "csv_path", "rate",
                        "max_concurrent", "duration_mode", "duration_s",
                        "duration_max_s", "match_key", "target_calls",
                        "target_minutes", "created_at", "started_at",
                        "stopped_at",
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

    def _row_to_campaign(self, row):
        keys = (
            "id", "name", "status", "node_id", "dest_host", "dest_port",
            "transport", "csv_path", "rate", "max_concurrent", "duration_mode",
            "duration_s", "duration_max_s", "match_key", "target_calls",
            "target_minutes", "created_at", "started_at", "stopped_at",
        )
        c = dict(zip(keys, row))
        c["instance_id"] = f"uac-{c['id']}"
        return c

    def _load_campaign(self, campaign_id):
        if self.db is None:
            return None
        try:
            from sqlalchemy import text

            with self.db.engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT id, name, status, node_id, dest_host, dest_port, "
                        "transport, csv_path, rate, max_concurrent, duration_mode, "
                        "duration_s, duration_max_s, match_key, target_calls, "
                        "target_minutes, created_at, started_at, stopped_at "
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
                        "SELECT id, name, status, node_id, dest_host, dest_port, "
                        "transport, csv_path, rate, max_concurrent, duration_mode, "
                        "duration_s, duration_max_s, match_key, target_calls, "
                        "target_minutes, created_at, started_at, stopped_at "
                        "FROM loop_campaigns ORDER BY created_at DESC"
                    )
                ).fetchall()
            return [self._row_to_campaign(r) for r in rows]
        except Exception as e:
            logger.warning("Could not list loop campaigns: %s", e)
            return None

    # ── CSV export of call_records (design §4.4) ─────────────────────────────

    # CSV-injection guard: a leading one of these makes spreadsheet apps treat
    # the cell as a formula. a_number/b_number/source_ip arrive off the wire, so
    # any cell starting with one is de-fanged with a leading apostrophe.
    _CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")

    @classmethod
    def _csv_safe(cls, value) -> str:
        """Neutralize formula injection in an exported CSV cell.

        Quoting is handled by ``csv.writer``; this only addresses the formula
        vector (a leading =/+/-/@ executing in Excel/Sheets) by prefixing such a
        cell with an apostrophe. Tab/CR/LF leads are treated the same way since
        a cell may be re-trimmed by the consumer.
        """
        if value is None:
            return ""
        s = str(value)
        if s and (s[0] in cls._CSV_FORMULA_PREFIXES or s[0] in ("\t", "\r", "\n")):
            return "'" + s
        return s

    def records_csv(self, campaign_id: str) -> str:
        """Export this campaign's ``call_records`` as a CSV string (header + rows).

        Uses ``csv.writer`` for correct RFC-4180 quoting (a field containing a
        comma/quote/newline is quoted, not naively joined) and de-fangs formula
        injection in attacker-influenced cells. Returns just the header row when
        there are no records (or no DB) so the endpoint always yields valid CSV.
        """
        columns = [
            "id", "campaign_id", "direction", "call_uuid", "a_number",
            "b_number", "source_ip", "t_start_ms", "t_answer_ms", "t_end_ms",
            "duration_ms", "final_code", "matched_record_id", "created_at",
        ]
        buf = io.StringIO()
        # \r\n line terminator is the CSV (RFC-4180) standard; quoting is QUOTE_MINIMAL.
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(columns)

        rows = []
        if self.db is not None:
            try:
                from sqlalchemy import text

                with self.db.engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            "SELECT " + ", ".join(columns) + " FROM call_records "
                            "WHERE campaign_id = :cid ORDER BY id"
                        ),
                        {"cid": campaign_id},
                    ).fetchall()
            except Exception as e:
                logger.warning("Could not export call_records for %s: %s",
                               campaign_id, e)
                rows = []

        for r in rows:
            writer.writerow([self._csv_safe(v) for v in r])
        return buf.getvalue()

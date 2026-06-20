"""
GenCall configuration manager.
Reads gencall.cfg and provides typed access to all settings.
"""

import configparser
import os
import logging

logger = logging.getLogger("gencall.config")

DEFAULT_CONFIG_PATH = "/opt/gencall/etc/gencall.cfg"
LOCAL_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "etc", "gencall.cfg")


class Config:
    _instance = None

    def __new__(cls, path=None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, path=None):
        if self._initialized:
            return
        self._parser = configparser.ConfigParser()
        self._path = path or self._find_config()
        if self._path:
            self._parser.read(self._path)
            logger.info("Config loaded from %s", self._path)
        else:
            logger.warning("No config file found, using defaults")
        self._validate()
        self._initialized = True

    def _validate(self):
        """Light, non-fatal validation of numeric settings.

        Bad values only emit a warning — the typed accessors still return
        whatever the parser/fallback yields, so behavior is unchanged. This is
        purely an early heads-up for operators with a misconfigured file.
        """
        def warn_range(label, value, low, high):
            try:
                v = int(value)
            except (TypeError, ValueError):
                logger.warning("Config: %s is not an integer (%r); using anyway", label, value)
                return
            if not (low <= v <= high):
                logger.warning(
                    "Config: %s=%d is outside the sane range [%d, %d]; using anyway",
                    label, v, low, high,
                )

        # TCP/UDP port numbers must be 1-65535.
        warn_range("web.port", self.web_port, 1, 65535)

        # RTP port window: each within range and min < max.
        min_rtp = self.min_rtp_port
        max_rtp = self.max_rtp_port
        warn_range("sip.min_rtp_port", min_rtp, 1, 65535)
        warn_range("sip.max_rtp_port", max_rtp, 1, 65535)
        try:
            if int(min_rtp) >= int(max_rtp):
                logger.warning(
                    "Config: sip.min_rtp_port (%s) should be less than sip.max_rtp_port (%s)",
                    min_rtp, max_rtp,
                )
        except (TypeError, ValueError):
            pass

        # SIP timers and intervals should be positive.
        if self.sip_t1 <= 0:
            logger.warning("Config: sip.T1=%s should be a positive number of ms", self.sip_t1)
        if self.sip_t2 <= 0:
            logger.warning("Config: sip.T2=%s should be a positive number of ms", self.sip_t2)
        if self.stats_interval <= 0:
            logger.warning(
                "Config: stats.interval=%s should be positive (seconds between polls)",
                self.stats_interval,
            )

        # SIPp file-descriptor limit should be a positive count.
        if self.sipp_file_limit <= 0:
            logger.warning(
                "Config: sipp.open_file_limit=%s should be a positive integer",
                self.sipp_file_limit,
            )

    @staticmethod
    def _find_config():
        for p in [os.environ.get("GENCALL_CONFIG", ""), LOCAL_CONFIG_PATH, DEFAULT_CONFIG_PATH]:
            if p and os.path.isfile(p):
                return p
        return None

    def get(self, section, key, fallback=None):
        return self._parser.get(section, key, fallback=fallback)

    def getint(self, section, key, fallback=0):
        return self._parser.getint(section, key, fallback=fallback)

    def getfloat(self, section, key, fallback=0.0):
        return self._parser.getfloat(section, key, fallback=fallback)

    def getbool(self, section, key, fallback=False):
        return self._parser.getboolean(section, key, fallback=fallback)

    # --- Web ---
    @property
    def web_host(self):
        return self.get("web", "host", "0.0.0.0")

    @property
    def web_port(self):
        return self.getint("web", "port", 8080)

    @property
    def web_ssl(self):
        return self.getbool("web", "ssl", False)

    @property
    def serve_console(self):
        """Whether this process serves the web console + live-stats WebSocket.

        Default True. Set [web] serve_console = false on FLEET WORKER boxes so
        they run headless (REST API + loop engine only) — the single controller
        GUI is the one pane of glass, and a worker skipping the console/WS
        broadcaster shaves idle CPU/RAM on the box.
        """
        import os
        if os.environ.get("GENCALL_HEADLESS"):
            return False
        return self.getbool("web", "serve_console", True)

    # --- Fleet (multi-box control plane over the VLAN) ---
    @property
    def fleet_announce(self):
        """Worker: broadcast a UDP discovery beacon on the VLAN so a controller
        can auto-register this box. Off by default (opt-in)."""
        return self.getbool("fleet", "announce", False)

    @property
    def fleet_discovery(self):
        """Controller: listen for worker beacons and auto-register them."""
        return self.getbool("fleet", "discovery", False)

    @property
    def fleet_token(self):
        """Shared secret carried in beacons AND used as the api_key the controller
        presents to auto-discovered workers. Same value on every box in the VLAN.
        A beacon whose token does not match is ignored (private-VLAN trust)."""
        return self.get("fleet", "token", "")

    @property
    def fleet_beacon_port(self):
        return self.getint("fleet", "beacon_port", 45790)

    @property
    def fleet_beacon_interval(self):
        return self.getint("fleet", "beacon_interval", 10)

    @property
    def fleet_node_address(self):
        """The base URL a worker advertises to controllers (e.g.
        http://10.20.8.11:8080). Empty => derived from web_host/web_port."""
        return self.get("fleet", "node_address", "")

    @property
    def ssl_cert(self):
        return self.get("web", "ssl_cert", "")

    @property
    def ssl_key(self):
        return self.get("web", "ssl_key", "")

    # --- SIP ---
    @property
    def sip_t1(self):
        return self.getint("sip", "T1", 60)

    @property
    def sip_t2(self):
        return self.getint("sip", "T2", 120)

    @property
    def sip_local_ip(self):
        """SIP-facing local address the UAS binds (signalling + media).

        Empty default ("") means "let SIPp bind all interfaces" — fine for a
        single-homed box. On a multi-homed deploy set [sip] local_ip to the
        MADA-facing address so the UAS's -i/-mi and the SDP it advertises match
        the interface return media actually arrives on.
        """
        return self.get("sip", "local_ip", "")

    @property
    def min_rtp_port(self):
        return self.getint("sip", "min_rtp_port", 10000)

    @property
    def max_rtp_port(self):
        return self.getint("sip", "max_rtp_port", 20000)

    # --- SIPp ---
    @property
    def sipp_command(self):
        return self.get("sipp", "command", "/usr/local/bin/sipp")

    @property
    def sipp_file_limit(self):
        return self.getint("sipp", "open_file_limit", 5000)

    @property
    def sipp_transport(self):
        return self.get("sipp", "default_transport", "udp")

    @property
    def sipp_stats_dir(self):
        # Directory for SIPp's per-instance stats CSV. Defaults to /tmp to
        # preserve existing Linux behavior; override on non-POSIX hosts (e.g.
        # Windows) or to relocate stats off /tmp on Linux containers.
        return self.get("sipp", "stats_dir", "/tmp")

    # --- Capture (on-demand pcap "trace", design Part 3) ---
    # On-demand tcpdump capture per running loop. tcpdump is Linux-only; the
    # capture endpoints fail cleanly (503) when it is absent. The watchdog caps
    # below bound a single forgotten capture's size/duration so it can't fill
    # the disk.
    @property
    def capture_command(self):
        """tcpdump binary used for on-demand pcap captures."""
        return self.get("capture", "command", "tcpdump")

    @property
    def capture_dir(self):
        """Where pcap captures are written (defaults to the sipp stats dir)."""
        return self.get("capture", "dir", "") or self.sipp_stats_dir

    @property
    def capture_max_seconds(self):
        """Auto-stop a capture after this many seconds (watchdog). 0 = no limit."""
        return self.getint("capture", "max_seconds", 300)

    @property
    def capture_max_mb(self):
        """Auto-stop a capture once its file exceeds this many MB. 0 = no limit."""
        return self.getint("capture", "max_mb", 100)

    @property
    def capture_snaplen(self):
        """tcpdump -s snaplen (0 = full packet)."""
        return self.getint("capture", "snaplen", 0)

    # --- Database ---
    # Secrets (DB credentials) should come from the environment, never the
    # config file. Env vars override the corresponding [database] settings.
    @property
    def db_engine(self):
        return os.environ.get("GENCALL_DB_ENGINE") or self.get("database", "engine", "sqlite")

    @property
    def db_url(self):
        # Full URL override wins (e.g. GENCALL_DATABASE_URL=postgresql://...).
        env_url = os.environ.get("GENCALL_DATABASE_URL")
        if env_url:
            return env_url

        if self.db_engine == "postgresql":
            user = os.environ.get("GENCALL_PG_USER") or self.get("database", "pg_user", "gencall")
            pw = os.environ.get("GENCALL_PG_PASSWORD") or self.get("database", "pg_password", "")
            host = os.environ.get("GENCALL_PG_HOST") or self.get("database", "pg_host", "127.0.0.1")
            port = os.environ.get("GENCALL_PG_PORT") or self.getint("database", "pg_port", 5432)
            db = os.environ.get("GENCALL_PG_DATABASE") or self.get("database", "pg_database", "gencall")
            return f"postgresql://{user}:{pw}@{host}:{port}/{db}"
        else:
            path = self.get("database", "sqlite_path", "/opt/gencall/etc/gencall.db")
            return f"sqlite:///{path}"

    # --- Logging ---
    @property
    def log_level(self):
        return self.getint("logging", "level", 20)

    @property
    def log_file(self):
        return self.get("logging", "file", "/opt/gencall/logs/gencall.log")

    # --- Media ---
    @property
    def media_path(self):
        return self.get("media", "path", "/opt/gencall/media")

    # --- Stats ---
    @property
    def stats_interval(self):
        return self.getint("stats", "interval", 5)

    @property
    def stats_history_size(self):
        return self.getint("stats", "history_size", 1000)

    # --- Loops (LoopEngine caps, design §4.1) ---
    # Conservative defaults tuned for the 4 vCPU / 4 GB deploy target. They cap
    # how much native SIPp work the (control-plane-only) engine will spawn so a
    # single box is never overcommitted: 50 concurrent loop campaigns (≈ 100
    # channels), 120 simultaneously-answered inbound calls, and a hard
    # answered-call ceiling of 7200 s so a wedged dialog can never pin a channel.
    @property
    def loops_max_concurrent(self):
        return self.getint("loops", "max_concurrent_loops", 50)

    @property
    def loops_max_answered(self):
        # UAS answer-side -l (simultaneous answered calls it will hold). Must stay
        # >= loops_max_channels, else a high-concurrency UAC outruns the answer
        # machine and calls past this never get answered/matched. Override with
        # [loops] max_answered_calls.
        return self.getint("loops", "max_answered_calls", 1100)

    @property
    def loops_answered_max_duration_s(self):
        return self.getint("loops", "answered_max_duration_s", 7200)

    # Minimum seconds between UAS restart attempts — the throttled backoff floor
    # for the answer-side monitor (design §8). Never busy-restart a crash loop.
    @property
    def loops_uas_restart_backoff_s(self):
        return self.getint("loops", "uas_restart_backoff_s", 5)

    # ── Loop input bounds (security: keep unbounded inputs from OOMing the box) ──
    # Per-campaign caps applied at the API/engine boundary so a single start can
    # never request an absurd channel/rate count that OOMs the box. The 1000-
    # channel default is the realistic ceiling for a 2-core/4 GB worker (measured:
    # ~0.85 MB + a sliver of a core per concurrent call with RTP echo); raise
    # [loops] max_channels on bigger boxes, but keep loops_max_answered >= it.
    @property
    def loops_max_rate_cps(self):
        """Hard ceiling on a campaign's call rate (calls per second)."""
        return self.getfloat("loops", "max_rate_cps", 500.0)

    @property
    def loops_max_channels(self):
        """Hard ceiling on a campaign's max_concurrent (per-campaign channels)."""
        return self.getint("loops", "max_channels", 1000)

    @property
    def loops_rtp_audio(self):
        """Raw A-law (G.711 PCMA) sample file SIPp's rtp_stream plays as the
        loop's media when RTP is enabled.

        rtp_stream streams over the normal media socket (no CAP_NET_RAW) and
        loops natively, so it replaced play_pcap_audio — which used a raw socket
        (needed setcap) and, looped via 37 unrolled plays, tore down ~30% of
        calls. The file is HEADERLESS codec samples (PT 8). Defaults to the
        bundled g711a.raw; override with ``[loops] rtp_audio``. Returns "" if
        missing, so the engine runs the loop signaling-only rather than failing.
        """
        explicit = self.get("loops", "rtp_audio", "")
        if explicit:
            return explicit
        import os
        bundled = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scenarios", "media", "g711a.raw",
        )
        return bundled if os.path.isfile(bundled) else ""

    @property
    def loops_max_duration_s(self):
        """Hard ceiling on a single call's hold duration (seconds)."""
        return self.getint("loops", "max_call_duration_s", 86400)

    # ── Loop destination allow-list (security: open SIP originator / SSRF) ──────
    # dest_host comes off the wire and flows to the SIPp target. Private,
    # loopback, multicast and 0.0.0.0 destinations are rejected by default so the
    # box can't be turned into an internal-network SIP originator/scanner. List
    # exact IPs or CIDRs here to explicitly permit otherwise-blocked ranges (e.g.
    # a MADA peer that legitimately lives on an RFC1918 lab network).
    @property
    def loops_dest_allowlist(self):
        """Allowed loop dest_host IPs/CIDRs that bypass the private/loopback block.

        Comma- or whitespace-separated; empty by default (no exceptions —
        private/loopback/multicast destinations are refused).
        """
        raw = self.get("loops", "dest_allowlist", "") or ""
        return [tok for tok in raw.replace(",", " ").split() if tok]

    # --- Trust filter / inbound whitelist (design §4.1) ---
    # The REAL security boundary is the host firewall (the deploy docs ship the
    # nftables/ufw rule set restricting UDP/5060 + the RTP range to these IPs).
    # The app layer is verification-only: the parser tags each inbound record
    # with its source_ip and flags/drops anything outside this list, so a
    # misconfigured firewall is *visible* rather than silently trusted.
    @property
    def trust_whitelist(self):
        """Allowed inbound SIP source IPs/CIDRs (MADA + any extras).

        Comma- or whitespace-separated in the config; returned as a list of
        non-empty tokens. Empty by default — an empty list means "nothing is
        verified as trusted" so a forgotten whitelist surfaces in the records
        (every inbound call flagged) rather than being silently accepted.
        """
        raw = self.get("trust", "whitelist", "") or ""
        return [tok for tok in raw.replace(",", " ").split() if tok]

    @property
    def trust_drop_untrusted(self):
        """Drop (vs. flag-and-keep) inbound records from outside the whitelist.

        Default False: a non-whitelisted inbound record is KEPT but flagged
        untrusted so a misconfigured firewall is visible in the records rather
        than silently discarded. Set [trust] drop_untrusted = true to drop them
        (the host firewall remains the real boundary either way, design §4.1).
        """
        return self.getbool("trust", "drop_untrusted", False)

    # --- Retention (design §5 retention, §7 stage 10) ---
    # The interval-gated pruner for the call_records growth table. Defaults tuned
    # for the 4 GB box; the interval gate (never per-iteration) is what keeps us
    # from rebuilding sigma's DELETE storm.
    @property
    def retention_call_records_days(self):
        """Delete call_records older than this many days (0 disables pruning)."""
        return self.getint("retention", "call_records_days", 30)

    @property
    def retention_interval_hours(self):
        """Minimum hours between two actual prunes — the interval gate."""
        return self.getint("retention", "interval_hours", 24)

    @classmethod
    def reset(cls):
        cls._instance = None

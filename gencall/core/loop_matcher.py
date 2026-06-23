"""
LoopMatcher (design §4.3 / §5).

Periodic DB job (every >= 10 s while a campaign runs) that joins outbound and
inbound ``call_records`` on the number pair within a configurable time window
(default 1 h) and writes a per-campaign ``loop_stats`` snapshot. It answers the
loop-accounting questions of §1: how many minutes went out (A-side), how many
came back (B-side), did the loop close (completion %), and what is the per-call
delta (in_ms - out_ms) distribution that is the deal's margin.

Matching is **heuristic by design** — the chain may rewrite numbers — so the
match key is configurable per campaign (``exact`` on b_number, or ``suffixN``
on the last N digits of b_number). The two structural rules from the spec:

  * **Completion %** is matched inbound / answered outbound. An outbound call
    that never answered (failure) is not part of the loop that should return, so
    the denominator is answered_out, not calls_out.
  * **Minutes-IN is matched inbound only.** ``minutes_in_ms`` sums the duration
    of the inbound legs that PAIRED to this campaign's outbound calls. The answer
    side (UAS) is shared across every campaign on the box and inbound records
    carry no campaign id, so a time-windowed "all inbound" sum attributes the
    same shared inbound minutes to every concurrent campaign at once (the 3x+
    per-campaign over-count seen on cy213, where global in≈out but each campaign
    reported ~3x its outbound). Matching is the only per-campaign attribution the
    inbound side has, so minutes-in counts matched legs. ``calls_in_matched`` is
    that pair count.

This is control-plane only: a single throttled DB query per pass, no per-record
loops in SQL, and the background scheduler sleeps >= ``MIN_INTERVAL_S`` between
passes (no busy loop, per this codebase's standard). The calls/media live in
native SIPp; this just reads what the tail-parser already wrote.

The matcher also stamps ``call_records.matched_record_id`` on both sides of each
matched pair so a record's pairing is persisted (and a re-run is idempotent —
re-pairing the same records yields the same stats).
"""

import bisect
import datetime
import json
import logging
import threading

logger = logging.getLogger("gencall.loop_matcher")

# Minimum interval between matcher passes. The spec mandates a periodic job of
# ">= 10 s while a campaign runs" (design §4.3); we floor any smaller request so
# the control plane never busy-matches.
MIN_INTERVAL_S = 10.0

# Default join window: inbound is matched to an outbound call only if it started
# within this many seconds of the outbound call (design §4.3, default 1 h).
DEFAULT_WINDOW_S = 3600

# Memory bound: a matcher pass only pairs the still-UNMATCHED outbound calls
# (matched ones are durable via call_records.matched_record_id and counted in
# SQL), and never scans more than this many per pass. This is what keeps RSS
# flat regardless of campaign age — the old code reloaded the campaign's ENTIRE
# outbound history every pass and leaked to GBs over days.
MAX_PAIR_SCAN = 20000
# Cap the unmatched-pairs list embedded in each loop_stats snapshot so a loop
# that never closes (e.g. all 408) can't bloat the row / WS payload unbounded.
MAX_UNMATCHED_REPORT = 200

# Delta histogram bucket edges in milliseconds (upper bounds; a final overflow
# bucket catches anything larger). Tuned for the §1 reference deltas (0.25–1 s).
_HIST_EDGES_MS = [0, 100, 250, 500, 750, 1000, 1500, 2000]


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _suffix_len(match_key):
    """Return the digit count for a ``suffixN`` match key, else None (= exact).

    ``"exact"`` (or anything not ``suffix<int>``) -> None. ``"suffix6"`` -> 6.
    A malformed suffix key falls back to exact rather than raising.
    """
    if not match_key:
        return None
    key = str(match_key).lower()
    if key.startswith("suffix"):
        try:
            n = int(key[len("suffix"):])
            return n if n > 0 else None
        except ValueError:
            return None
    return None


def match_value(b_number, match_key):
    """Normalize a record's b_number to its match key.

    Exact match keys on the whole b_number; ``suffixN`` keys on the last N
    digits (so a chain that rewrites leading digits still matches). Returns None
    for an empty number so a record with no b_number never matches another.
    """
    if not b_number:
        return None
    n = _suffix_len(match_key)
    if n is None:
        return b_number
    return b_number[-n:]


def _percentile(sorted_values, pct):
    """Nearest-rank percentile of an already-sorted, non-empty list.

    ``pct`` in [0, 100]. Returns a float. Uses the nearest-rank method (no
    interpolation) — simple and stable for the small per-campaign sample sizes
    and good enough for the dispute-resolution display this feeds.
    """
    if not sorted_values:
        return 0.0
    if pct <= 0:
        return float(sorted_values[0])
    if pct >= 100:
        return float(sorted_values[-1])
    # Nearest-rank: rank = ceil(pct/100 * n), 1-based.
    rank = -(-len(sorted_values) * pct // 100)  # ceil division
    rank = max(1, min(rank, len(sorted_values)))
    return float(sorted_values[rank - 1])


def _is_success_code(code):
    return code is not None and 200 <= code < 300


def _start_ms(r):
    """Best available SIPp [clock_tick] start (process-relative; fallback only).

    Outbound rows have no t_start_ms (the UAC logs at 200-OK and BYE, not at the
    INVITE), so fall back to t_answer_ms — every answered call has one. NOTE these
    are per-process ticks and are NOT comparable across SIPp processes; use
    ``_event_ms`` for any cross-record windowing/matching.
    """
    return r["t_start_ms"] if r["t_start_ms"] is not None else r["t_answer_ms"]


def _to_wall_ms(created_at):
    """Parse an ISO-8601 ``created_at`` string into epoch milliseconds, or None.

    ``created_at`` is stamped by the single tail-parser process on every record
    (out and in alike), so it is the one timestamp comparable ACROSS the separate
    SIPp processes that produced the legs.
    """
    if not created_at:
        return None
    try:
        dt = datetime.datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_iso(ms):
    """Epoch milliseconds -> UTC ISO-8601 string (for created_at range bounds)."""
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


def _event_ms(r):
    """Cross-process-comparable match/window key (epoch ms).

    The SIPp [clock_tick] timestamps (t_start_ms/t_answer_ms) are RELATIVE to each
    SIPp process's own start, so they cannot be compared across processes: every
    per-campaign UAC starts near 0 while the long-lived answer UAS can be hours in
    (ticks in the tens of millions). Windowing/matching on them paired a fresh
    outbound (tick ~ seconds) against an inbound from a 40 h-old UAS (tick ~ tens
    of millions) at a false distance of HOURS, rejecting every real pair — the
    ``calls_in_matched == 0`` bug seen on cy214. ``created_at`` is one parser
    clock, so it is the only comparable time. Fall back to the process tick only
    when created_at is absent (legacy rows) so an all-tick test set still windows
    internally.
    """
    w = _to_wall_ms(r.get("created_at"))
    return w if w is not None else _start_ms(r)


def _histogram(deltas_ms):
    """Bucket per-call deltas into a small fixed-edge histogram.

    Returns a list of ``{"lt_ms": edge|null, "count": n}`` dicts: each bucket
    counts deltas strictly below ``lt_ms`` and >= the previous edge; the final
    bucket (``lt_ms: null``) is the overflow for anything >= the last edge.
    """
    edges = _HIST_EDGES_MS
    counts = [0] * (len(edges) + 1)
    for d in deltas_ms:
        idx = bisect.bisect_right(edges, d)
        # bisect_right returns insertion point; map into our bucket layout where
        # bucket i = [edges[i-1], edges[i]) and the last bucket is overflow.
        counts[idx] += 1
    buckets = []
    prev = None
    for i, edge in enumerate(edges):
        buckets.append({"ge_ms": prev, "lt_ms": edge, "count": counts[i]})
        prev = edge
    buckets.append({"ge_ms": prev, "lt_ms": None, "count": counts[-1]})
    return buckets


class LoopMatcher:
    """Joins out/in ``call_records`` into per-campaign ``loop_stats``.

    ``db`` is a ``gencall.db.models.Database`` (required for any real work; with
    ``db=None`` the matcher is inert and ``match_campaign`` returns an empty
    stats dict). ``on_stats`` is an optional callback invoked with each computed
    stats dict after persistence — wired to the WebSocket ``loops`` topic so the
    console gets live snapshots (design §4.4). ``window_s`` is the default join
    window; a campaign may carry its own (not yet, but the signature allows it).

    The matcher does not own a campaign list; the caller (the engine/scheduler)
    tells it which campaign + match_key to match each pass. This keeps it a pure
    accounting function with one background scheduler thread on top.
    """

    def __init__(self, db=None, on_stats=None, window_s=DEFAULT_WINDOW_S,
                 interval_s=MIN_INTERVAL_S):
        self.db = db
        self.on_stats = on_stats
        self.window_s = int(window_s)
        # Floor the interval at the mandated minimum — never busy-match (§4.3).
        self.interval_s = max(float(interval_s), MIN_INTERVAL_S)
        # campaign_id -> match_key, the live set this matcher iterates each pass.
        self._campaigns = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ── tracked-campaign registration ────────────────────────────────────────

    def track(self, campaign_id, match_key="exact"):
        """Track a campaign so the background loop matches it each pass."""
        with self._lock:
            self._campaigns[campaign_id] = match_key or "exact"

    def untrack(self, campaign_id):
        """Stop matching a campaign (e.g. it stopped)."""
        with self._lock:
            self._campaigns.pop(campaign_id, None)

    # ── core matching (pure, DB-read) ────────────────────────────────────────

    _COLS = (
        "id", "campaign_id", "direction", "call_uuid", "a_number",
        "b_number", "source_ip", "t_start_ms", "t_answer_ms", "t_end_ms",
        "duration_ms", "final_code", "created_at",
    )

    def _pair_unmatched(self, campaign_id, match_key, window_ms):
        """Pair this campaign's still-UNMATCHED answered outbound calls to inbound
        legs, stamp ``matched_record_id`` on both sides (durable), and return
        ``(matched_pairs, unmatched_out, in_dicts)``.

        Only UNMATCHED rows are scanned (``matched_record_id IS NULL``) and the
        scan is capped at ``MAX_PAIR_SCAN`` — matched pairs are already durable
        and counted via SQL in ``_aggregate``, so a campaign that has run for days
        never reloads its whole history (the GB-scale leak). Inbound is scoped to
        the outbound wall-clock window so a shared UAS can't be double-counted.
        """
        from sqlalchemy import text
        sel = ", ".join(self._COLS)
        keys = list(self._COLS)
        with self.db.engine.begin() as conn:
            out_rows = conn.execute(
                text(
                    f"SELECT {sel} FROM call_records "
                    "WHERE campaign_id = :cid AND direction = 'out' "
                    "AND matched_record_id IS NULL "
                    "AND final_code >= 200 AND final_code < 300 "
                    "ORDER BY id DESC LIMIT :lim"
                ),
                {"cid": campaign_id, "lim": MAX_PAIR_SCAN},
            ).fetchall()
            out_dicts = [dict(zip(keys, r)) for r in out_rows]
            if not out_dicts:
                return [], [], []
            out_starts = [s for s in (_event_ms(o) for o in out_dicts) if s is not None]
            if not out_starts:
                return [], out_dicts, []
            floor_ms = min(out_starts) - window_ms
            ceil_ms = max(out_starts) + window_ms

            in_rows = conn.execute(
                text(
                    f"SELECT {sel} FROM call_records "
                    "WHERE direction = 'in' AND matched_record_id IS NULL "
                    "AND created_at >= :floor AND created_at <= :ceil "
                    "ORDER BY id DESC LIMIT :lim"
                ),
                {"floor": _ms_to_iso(floor_ms), "ceil": _ms_to_iso(ceil_ms),
                 "lim": MAX_PAIR_SCAN},
            ).fetchall()
            in_dicts = [dict(zip(keys, r)) for r in in_rows]

            # Greedy nearest-in-time pairing by match value (same rule as before).
            in_by_key = {}
            for r in in_dicts:
                mv = match_value(r["b_number"], match_key)
                if mv is None:
                    continue
                in_by_key.setdefault(mv, []).append(r)
            consumed = set()
            matched_pairs = []
            matched_out_ids = set()
            for o in out_dicts:
                mv = match_value(o["b_number"], match_key)
                if mv is None:
                    continue
                candidates = in_by_key.get(mv)
                if not candidates:
                    continue
                o_start = _event_ms(o)
                best = None
                best_dist = None
                for cand in candidates:
                    if cand["id"] in consumed:
                        continue
                    c_start = _event_ms(cand)
                    if o_start is not None and c_start is not None:
                        dist = abs(c_start - o_start)
                        if dist > window_ms:
                            continue
                    else:
                        dist = window_ms
                    if best is None or dist < best_dist:
                        best = cand
                        best_dist = dist
                if best is not None:
                    consumed.add(best["id"])
                    matched_pairs.append((o, best))
                    matched_out_ids.add(o["id"])

            # Durable stamp on both legs (idempotent; lets _aggregate count via SQL).
            for o, i in matched_pairs:
                conn.execute(
                    text("UPDATE call_records SET matched_record_id = :other "
                         "WHERE id = :id"), {"other": i["id"], "id": o["id"]})
                conn.execute(
                    text("UPDATE call_records SET matched_record_id = :other "
                         "WHERE id = :id"), {"other": o["id"], "id": i["id"]})

        unmatched_out = [o for o in out_dicts if o["id"] not in matched_out_ids]
        return matched_pairs, unmatched_out, in_dicts

    def _aggregate(self, campaign_id):
        """Cumulative per-campaign totals via SQL aggregates — no row loading, so
        cost is O(index) not O(rows). ``calls_in_matched`` / ``minutes_in_ms``
        read the durable ``matched_record_id`` pairing (minutes-in is the matched
        INBOUND legs only, JOIN-ed, so a shared UAS is never over-counted).
        """
        from sqlalchemy import text
        with self.db.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT COUNT(*), "
                    "COALESCE(SUM(CASE WHEN final_code>=200 AND final_code<300 "
                    "  THEN 1 ELSE 0 END),0), "
                    "COALESCE(SUM(CASE WHEN final_code>=200 AND final_code<300 "
                    "  THEN COALESCE(duration_ms,0) ELSE 0 END),0), "
                    "COALESCE(SUM(CASE WHEN matched_record_id IS NOT NULL "
                    "  THEN 1 ELSE 0 END),0) "
                    "FROM call_records WHERE campaign_id=:cid AND direction='out'"
                ), {"cid": campaign_id},
            ).fetchone()
            calls_out, answered_out, minutes_out_ms, calls_in_matched = (
                int(x or 0) for x in row)
            min_in = conn.execute(
                text(
                    "SELECT COALESCE(SUM(i.duration_ms),0) FROM call_records o "
                    "JOIN call_records i ON i.id = o.matched_record_id "
                    "WHERE o.campaign_id=:cid AND o.direction='out' "
                    "AND o.matched_record_id IS NOT NULL"
                ), {"cid": campaign_id},
            ).fetchone()
            minutes_in_ms = int((min_in[0] if min_in else 0) or 0)
            frows = conn.execute(
                text(
                    "SELECT final_code, COUNT(*) FROM call_records "
                    "WHERE campaign_id=:cid AND direction='out' "
                    "AND final_code IS NOT NULL "
                    "AND NOT (final_code>=200 AND final_code<300) "
                    "GROUP BY final_code"
                ), {"cid": campaign_id},
            ).fetchall()
            failures_out = {str(c): int(n) for c, n in frows}
        return {
            "calls_out": calls_out, "answered_out": answered_out,
            "minutes_out_ms": minutes_out_ms, "calls_in_matched": calls_in_matched,
            "minutes_in_ms": minutes_in_ms, "failures_out": failures_out,
        }

    def minutes_out_today_ms(self, campaign_id) -> int:
        """Answered-outbound minutes (ms) since 00:00 GMT today — the per-GMT-day
        figure the UI's daily target bar measures against.

        Computed live, never snapshotted: a per-day value would go stale across
        the GMT-midnight rollover. Mirrors _aggregate's answered-outbound sum with
        a ``created_at >= today-00:00-UTC`` floor (created_at is a UTC ISO-8601
        string, so a lexical ``>=`` is a chronological one)."""
        if self.db is None:
            return 0
        floor = datetime.datetime.now(datetime.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()
        from sqlalchemy import text
        try:
            with self.db.engine.connect() as conn:
                row = conn.execute(
                    text("SELECT COALESCE(SUM(CASE WHEN final_code>=200 AND "
                         "final_code<300 THEN COALESCE(duration_ms,0) ELSE 0 END),0) "
                         "FROM call_records WHERE campaign_id=:cid "
                         "AND direction='out' AND created_at >= :floor"),
                    {"cid": campaign_id, "floor": floor},
                ).fetchone()
            return int((row[0] if row else 0) or 0)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Could not compute today's minutes for %s: %s",
                           campaign_id, e)
            return 0

    def match_campaign(self, campaign_id, match_key="exact", window_s=None):
        """Match one campaign's records and return (and persist) its stats dict.

        Pairs each answered outbound call to the closest-in-time inbound call
        whose match value is equal and whose start is within ``window_s`` of the
        outbound start. Each inbound call is paired at most once (greedy nearest
        match). Computes the §4.3 aggregates and writes a ``loop_stats`` row.

        With no DB, returns an empty (all-zero) stats dict without persisting.
        """
        if self.db is None:
            return self._empty_stats(campaign_id)

        window_ms = (self.window_s if window_s is None else int(window_s)) * 1000

        # 1) Pair only the still-UNMATCHED recent rows (bounded) and stamp the
        #    durable matched_record_id on both legs. This is the whole leak fix:
        #    we never reload the campaign's full history into Python anymore.
        matched_pairs, unmatched_out, in_dicts = self._pair_unmatched(
            campaign_id, match_key, window_ms)

        # 2) Cumulative totals via SQL aggregates over the durable pairing — these
        #    stay lifetime-accurate (Minutes OUT/IN, Calls, completion) at O(index)
        #    cost, reflecting the pairs just stamped in step 1.
        agg = self._aggregate(campaign_id)
        calls_out = agg["calls_out"]
        answered_out = agg["answered_out"]
        minutes_out_ms = agg["minutes_out_ms"]
        calls_in_matched = agg["calls_in_matched"]
        minutes_in_ms = agg["minutes_in_ms"]
        failures_out = agg["failures_out"]

        # 3) Per-call delta (in_ms - out_ms) over the pairs matched THIS pass — a
        #    live latency sample (recent), which is what the dispute view wants.
        deltas = [((i["duration_ms"] or 0) - (o["duration_ms"] or 0))
                  for o, i in matched_pairs]
        deltas_sorted = sorted(deltas)
        delta_avg = (sum(deltas) / len(deltas)) if deltas else 0.0
        delta_p50 = _percentile(deltas_sorted, 50)
        delta_p95 = _percentile(deltas_sorted, 95)

        # ── completion %: matched inbound / answered outbound (both cumulative) ─
        completion_pct = (
            (calls_in_matched / answered_out * 100.0) if answered_out else 0.0
        )

        # ── unmatched pairs: recent answered-out with no return yet, capped ────
        unmatched_pairs = [
            {"a_number": o["a_number"], "b_number": o["b_number"],
             "call_uuid": o["call_uuid"]}
            for o in unmatched_out
        ][:MAX_UNMATCHED_REPORT]

        # ── inbound failures over the recent (unmatched) inbound window ────────
        failures_in = self._failures_by_code(in_dicts)

        stats = {
            "campaign_id": campaign_id,
            "ts": _now_iso(),
            "calls_out": calls_out,
            "answered_out": answered_out,
            "minutes_out_ms": minutes_out_ms,
            "calls_in_matched": calls_in_matched,
            "minutes_in_ms": minutes_in_ms,
            "completion_pct": round(completion_pct, 2),
            "delta_avg_ms": round(delta_avg, 2),
            "delta_p50_ms": delta_p50,
            "delta_p95_ms": delta_p95,
            "failures": {
                "out": failures_out,
                "in": failures_in,
            },
            "delta_histogram": _histogram(deltas),
            "unmatched_pairs": unmatched_pairs,
        }

        self._persist_stats(stats, matched_pairs)
        if self.on_stats is not None:
            try:
                self.on_stats(stats)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("loop-stats callback failed: %s", e)
        return stats

    @staticmethod
    def _failures_by_code(rows):
        """Count records whose final_code is a non-2xx failure, keyed by code.

        Returns a ``{code(str): count}`` dict. A record with no code or a 2xx
        code is a success and not counted; everything else (404/487/503/...) is
        a failure tallied by its code so the console can show per-code spikes.
        """
        out = {}
        for r in rows:
            code = r["final_code"]
            if code is None or _is_success_code(code):
                continue
            out[str(code)] = out.get(str(code), 0) + 1
        return out

    @staticmethod
    def _empty_stats(campaign_id):
        return {
            "campaign_id": campaign_id,
            "ts": _now_iso(),
            "calls_out": 0,
            "answered_out": 0,
            "minutes_out_ms": 0,
            "calls_in_matched": 0,
            "minutes_in_ms": 0,
            "completion_pct": 0.0,
            "delta_avg_ms": 0.0,
            "delta_p50_ms": 0.0,
            "delta_p95_ms": 0.0,
            "failures": {"out": {}, "in": {}},
            "delta_histogram": _histogram([]),
            "unmatched_pairs": [],
        }

    # ── persistence ──────────────────────────────────────────────────────────

    def _persist_stats(self, stats, matched_pairs):
        """Insert a loop_stats snapshot and stamp matched_record_id on both sides.

        The ``failures``/``delta_histogram``/``unmatched_pairs`` blocks are
        serialized into ``failures_json`` (one JSON column keeps the row schema
        small per §5). Stamping ``matched_record_id`` makes the pairing durable
        and lets a CSV export show which records closed the loop.
        """
        if self.db is None:
            return
        try:
            from sqlalchemy import text

            failures_json = json.dumps({
                "failures_out": stats["failures"]["out"],
                "failures_in": stats["failures"]["in"],
                "delta_histogram": stats["delta_histogram"],
                "unmatched_pairs": stats["unmatched_pairs"],
            })
            with self.db.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO loop_stats "
                        "(campaign_id, ts, calls_out, answered_out, "
                        " minutes_out_ms, calls_in_matched, minutes_in_ms, "
                        " completion_pct, delta_avg_ms, delta_p50_ms, "
                        " delta_p95_ms, failures_json) "
                        "VALUES (:campaign_id, :ts, :calls_out, :answered_out, "
                        " :minutes_out_ms, :calls_in_matched, :minutes_in_ms, "
                        " :completion_pct, :delta_avg_ms, :delta_p50_ms, "
                        " :delta_p95_ms, :failures_json)"
                    ),
                    {
                        "campaign_id": stats["campaign_id"],
                        "ts": stats["ts"],
                        "calls_out": stats["calls_out"],
                        "answered_out": stats["answered_out"],
                        "minutes_out_ms": stats["minutes_out_ms"],
                        "calls_in_matched": stats["calls_in_matched"],
                        "minutes_in_ms": stats["minutes_in_ms"],
                        "completion_pct": stats["completion_pct"],
                        "delta_avg_ms": stats["delta_avg_ms"],
                        "delta_p50_ms": stats["delta_p50_ms"],
                        "delta_p95_ms": stats["delta_p95_ms"],
                        "failures_json": failures_json,
                    },
                )
                # Stamp the pairing on both records (idempotent across re-runs).
                for o, i in matched_pairs:
                    conn.execute(
                        text("UPDATE call_records SET matched_record_id = :other "
                             "WHERE id = :id"),
                        {"other": i["id"], "id": o["id"]},
                    )
                    conn.execute(
                        text("UPDATE call_records SET matched_record_id = :other "
                             "WHERE id = :id"),
                        {"other": o["id"], "id": i["id"]},
                    )
        except Exception as e:
            logger.warning("Could not persist loop_stats for %s: %s",
                           stats.get("campaign_id"), e)

    def latest_stats(self, campaign_id):
        """Return the latest persisted loop_stats snapshot for a campaign.

        Reads the most recent row (max id) and re-expands ``failures_json`` into
        the structured ``failures`` / ``delta_histogram`` / ``unmatched_pairs``
        fields so the API returns the same shape the matcher produced. Returns
        None when there is no DB or no snapshot yet.
        """
        if self.db is None:
            return None
        try:
            from sqlalchemy import text

            with self.db.engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT campaign_id, ts, calls_out, answered_out, "
                        "minutes_out_ms, calls_in_matched, minutes_in_ms, "
                        "completion_pct, delta_avg_ms, delta_p50_ms, "
                        "delta_p95_ms, failures_json "
                        "FROM loop_stats WHERE campaign_id = :cid "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"cid": campaign_id},
                ).fetchone()
        except Exception as e:
            logger.warning("Could not read loop_stats for %s: %s", campaign_id, e)
            return None
        if row is None:
            return None
        extra = {}
        if row[11]:
            try:
                extra = json.loads(row[11])
            except (TypeError, ValueError):
                extra = {}
        return {
            "campaign_id": row[0],
            "ts": row[1],
            "calls_out": row[2],
            "answered_out": row[3],
            "minutes_out_ms": row[4],
            "minutes_out_today_ms": self.minutes_out_today_ms(row[0]),
            "calls_in_matched": row[5],
            "minutes_in_ms": row[6],
            "completion_pct": row[7],
            "delta_avg_ms": row[8],
            "delta_p50_ms": row[9],
            "delta_p95_ms": row[10],
            "failures": {
                "out": extra.get("failures_out", {}),
                "in": extra.get("failures_in", {}),
            },
            "delta_histogram": extra.get("delta_histogram", []),
            "unmatched_pairs": extra.get("unmatched_pairs", []),
        }

    # ── background scheduler (throttled, >= 10 s — no busy poll) ──────────────

    def start(self):
        """Start the background matcher thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="loop-matcher"
        )
        self._thread.start()

    def stop(self, timeout=5.0):
        """Signal the loop to exit and join the thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self):
        # Event-driven sleep: wakes early only on stop(), otherwise idles for the
        # full (>= 10 s) interval so the control plane stays near-zero CPU.
        while not self._stop.is_set():
            try:
                with self._lock:
                    targets = list(self._campaigns.items())
                for campaign_id, match_key in targets:
                    self.match_campaign(campaign_id, match_key=match_key)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Loop-matcher pass failed: %s", e)
            self._stop.wait(self.interval_s)

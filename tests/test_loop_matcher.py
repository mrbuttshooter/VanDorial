"""
LoopMatcher tests (design §4.3 / §5, build stage "Matcher").

Covers, with no real SIPp/Docker/Linux (SQLite only — synthetic call_records
inserted directly), the spec's required matcher behaviors:

  * correct completion_pct (matched inbound / answered outbound),
  * minutes out (A-side) and minutes in (B-side, MATCHED inbound legs only),
  * per-call delta avg / p50 / p95 + the small histogram,
  * suffix matching when the chain rewrites leading digits,
  * window edge handling (just-inside vs just-outside the join window),
  * unmatched inbound is EXCLUDED from minutes_in (shared answer side, no cid),
  * failures by SIP code, outbound and inbound separately,
  * the latest_stats round-trip the GET /api/loops/{id} endpoint reads.

The matcher's background loop is exercised by calling match_campaign() directly,
so tests never depend on the >= 10 s scheduler sleep.
"""

import pytest

from gencall.core.loop_matcher import (
    DEFAULT_WINDOW_S,
    LoopMatcher,
    _percentile,
    match_value,
)
from gencall.db.migrations import apply_migrations
from gencall.db.models import Database


# ── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    """Temp SQLite Database with ORM tables + plain SQL migrations applied."""
    database = Database(f"sqlite:///{tmp_path / 'matcher.db'}")
    database.create_tables()
    apply_migrations(database.engine)
    return database


def _insert_record(db, *, campaign_id, direction, call_uuid, a_number,
                   b_number, t_start_ms, duration_ms, final_code=200,
                   source_ip=None, created_at=None):
    """Insert one synthetic call_record. t_answer/t_end are derived so the
    neutral duration math (t_end - t_answer) reproduces ``duration_ms``.

    ``created_at`` (the wall-clock the matcher now windows/ranks on) defaults to a
    UTC ISO string derived from t_start_ms, so a test that expresses call timing
    via t_start_ms expresses the SAME timing on the matcher's wall-clock basis.
    """
    import datetime as _dt
    from sqlalchemy import text

    t_answer = t_start_ms + 50 if final_code and 200 <= final_code < 300 else None
    t_end = (t_answer + duration_ms) if t_answer is not None else None
    if created_at is None:
        created_at = _dt.datetime.fromtimestamp(
            (t_start_ms or 0) / 1000, _dt.timezone.utc).isoformat()
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO call_records "
                "(campaign_id, direction, call_uuid, a_number, b_number, "
                " source_ip, t_start_ms, t_answer_ms, t_end_ms, duration_ms, "
                " final_code, created_at) "
                "VALUES (:campaign_id, :direction, :call_uuid, :a_number, "
                " :b_number, :source_ip, :t_start_ms, :t_answer_ms, :t_end_ms, "
                " :duration_ms, :final_code, :created_at)"
            ),
            {
                "campaign_id": campaign_id,
                "direction": direction,
                "call_uuid": call_uuid,
                "a_number": a_number,
                "b_number": b_number,
                "source_ip": source_ip,
                "t_start_ms": t_start_ms,
                "t_answer_ms": t_answer,
                "t_end_ms": t_end,
                "duration_ms": duration_ms if final_code and 200 <= final_code < 300 else 0,
                "final_code": final_code,
                "created_at": created_at,
            },
        )


CID = "loop-test01"


# ── match-key normalization ─────────────────────────────────────────────────


def test_match_value_exact_and_suffix():
    assert match_value("4477001234", "exact") == "4477001234"
    # suffix6 keys on the last 6 digits — survives a leading-digit rewrite.
    assert match_value("4477001234", "suffix6") == "001234"
    assert match_value("00999001234", "suffix6") == "001234"
    # empty number never produces a key (so it cannot match another record).
    assert match_value("", "exact") is None
    # malformed suffix key falls back to exact.
    assert match_value("123456", "suffixZ") == "123456"


def test_matches_outbound_with_null_t_start_ms(db):
    """Real loop UAC rows carry NO t_start_ms (the scenario logs at 200-OK and BYE,
    not at the INVITE) — only t_answer_ms. The matcher must fall back to
    t_answer_ms for both the inbound window and the nearest-match, otherwise it
    loads zero inbound and matches nothing (the bug seen on the box: minutes_out
    counted but calls_in_matched=0)."""
    from sqlalchemy import text

    base = 1_700_000_000_000
    with db.engine.begin() as conn:
        # outbound: t_start_ms NULL, t_answer_ms set (mirrors loop_uac.xml)
        conn.execute(
            text("INSERT INTO call_records (campaign_id,direction,call_uuid,a_number,"
                 "b_number,source_ip,t_start_ms,t_answer_ms,t_end_ms,duration_ms,"
                 "final_code,created_at) VALUES (:c,'out','out-1','1000','2000',NULL,"
                 "NULL,:ans,:end,:dur,200,'2026-01-01T00:00:00+00:00')"),
            {"c": CID, "ans": base, "end": base + 3000, "dur": 3000})
        # inbound returns ~4 s later (loop latency), t_start_ms set
        conn.execute(
            text("INSERT INTO call_records (campaign_id,direction,call_uuid,a_number,"
                 "b_number,source_ip,t_start_ms,t_answer_ms,t_end_ms,duration_ms,"
                 "final_code,created_at) VALUES (NULL,'in','in-1','1000','2000',"
                 "'127.0.0.1',:st,:st,:end,:dur,200,'2026-01-01T00:00:00+00:00')"),
            {"st": base + 4000, "end": base + 7000, "dur": 3000})

    stats = LoopMatcher(db=db).match_campaign(CID, match_key="exact")
    assert stats["calls_out"] == 1
    assert stats["answered_out"] == 1
    assert stats["calls_in_matched"] == 1, "inbound must match even when out t_start_ms is NULL"
    assert stats["minutes_in_ms"] == 3000
    assert stats["completion_pct"] == 100.0


def test_matches_across_process_clock_drift(db):
    """Out leg (fresh per-campaign UAC) and return leg (long-lived answer UAS)
    carry SIPp [clock_tick] timestamps from DIFFERENT process epochs: the UAC's
    are ~seconds, the UAS's ~tens of millions after ~40 h up. Matching MUST key on
    the wall-clock created_at, not the raw tick — otherwise the pair sits a false
    ~40 h apart and is rejected by the join window. This is the calls_in_matched=0
    bug observed on cy214 once the answer UAS had been running for a day (the loop
    physically closed 16/16 but reported 0% completion)."""
    from sqlalchemy import text
    import datetime as _dt

    wall = _dt.datetime(2026, 6, 13, 18, 0, 0, tzinfo=_dt.timezone.utc)
    out_created = wall.isoformat()
    in_created = (wall + _dt.timedelta(seconds=2)).isoformat()  # ~loop latency
    with db.engine.begin() as conn:
        # fresh UAC: tiny ticks (process up a few seconds), no t_start (logs at 200/BYE)
        conn.execute(text(
            "INSERT INTO call_records (campaign_id,direction,call_uuid,a_number,"
            "b_number,source_ip,t_start_ms,t_answer_ms,t_end_ms,duration_ms,"
            "final_code,created_at) VALUES (:c,'out','o1','97430500001','224626500001',"
            "NULL,NULL,5000,65000,60000,200,:ca)"),
            {"c": CID, "ca": out_created})
        # long-lived UAS: huge ticks (~43 h up ≈ 156e6 ms)
        conn.execute(text(
            "INSERT INTO call_records (campaign_id,direction,call_uuid,a_number,"
            "b_number,source_ip,t_start_ms,t_answer_ms,t_end_ms,duration_ms,"
            "final_code,created_at) VALUES (NULL,'in','i1','97430500001','224626500001',"
            "'10.0.0.1',156000000,156000050,156060100,60050,200,:ca)"),
            {"ca": in_created})

    stats = LoopMatcher(db=db).match_campaign(CID, match_key="exact")
    assert stats["calls_out"] == 1
    assert stats["answered_out"] == 1
    assert stats["calls_in_matched"] == 1, "must pair on wall-clock, not the process tick"
    assert stats["completion_pct"] == 100.0
    assert stats["minutes_in_ms"] == 60050


def test_percentile_nearest_rank():
    vals = [10, 20, 30, 40, 100]
    assert _percentile(sorted(vals), 50) == 30
    assert _percentile(sorted(vals), 95) == 100
    assert _percentile([], 50) == 0.0


# ── completion %, minutes out/in, delta percentiles ─────────────────────────


def test_full_loop_completion_and_minutes(db):
    """3 answered outbound, all returned: 100 % completion; minutes summed;
    per-call delta (in - out) percentiles computed over the matched pairs."""
    # Outbound A-side durations (ms) and matching inbound B-side durations.
    # Deltas (in - out): 200, 250, 300 ms.
    out_durs = [60000, 60000, 60000]
    in_durs = [60200, 60250, 60300]
    base = 1_700_000_000_000
    for i, (od, idur) in enumerate(zip(out_durs, in_durs)):
        b = f"44770012{i:02d}"
        _insert_record(db, campaign_id=CID, direction="out",
                       call_uuid=f"out-{i}", a_number="1000", b_number=b,
                       t_start_ms=base + i * 1000, duration_ms=od)
        _insert_record(db, campaign_id=None, direction="in",
                       call_uuid=f"in-{i}", a_number=b, b_number=b,
                       t_start_ms=base + i * 1000 + 500, duration_ms=idur,
                       source_ip="10.0.0.1")

    matcher = LoopMatcher(db=db)
    stats = matcher.match_campaign(CID, match_key="exact")

    assert stats["calls_out"] == 3
    assert stats["answered_out"] == 3
    assert stats["calls_in_matched"] == 3
    assert stats["completion_pct"] == 100.0
    assert stats["minutes_out_ms"] == sum(out_durs)
    assert stats["minutes_in_ms"] == sum(in_durs)
    # Deltas 200/250/300: avg 250, p50 250, p95 300 (nearest-rank).
    assert stats["delta_avg_ms"] == 250.0
    assert stats["delta_p50_ms"] == 250
    assert stats["delta_p95_ms"] == 300
    assert stats["unmatched_pairs"] == []


def test_partial_completion(db):
    """2 of 4 answered outbound returned -> 50 % completion; 2 unmatched pairs."""
    base = 1_700_000_000_000
    for i in range(4):
        b = f"55500{i:03d}"
        _insert_record(db, campaign_id=CID, direction="out",
                       call_uuid=f"out-{i}", a_number="1000", b_number=b,
                       t_start_ms=base + i * 1000, duration_ms=30000)
    # Only the first two return.
    for i in range(2):
        b = f"55500{i:03d}"
        _insert_record(db, campaign_id=None, direction="in",
                       call_uuid=f"in-{i}", a_number=b, b_number=b,
                       t_start_ms=base + i * 1000 + 400, duration_ms=30100)

    stats = LoopMatcher(db=db).match_campaign(CID, match_key="exact")
    assert stats["answered_out"] == 4
    assert stats["calls_in_matched"] == 2
    assert stats["completion_pct"] == 50.0
    assert len(stats["unmatched_pairs"]) == 2
    unmatched_bs = {p["b_number"] for p in stats["unmatched_pairs"]}
    assert unmatched_bs == {"55500002", "55500003"}


# ── suffix matching when the chain rewrites leading digits ───────────────────


def test_suffix_matching_rewritten_leading_digits(db):
    """The carrier chain rewrites the leading digits of B; suffix6 still pairs
    the returning leg to the originated call."""
    base = 1_700_000_000_000
    # Outbound dials 44 77 00 1234; inbound arrives as 00 999 00 1234.
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="out-0",
                   a_number="1000", b_number="4477001234",
                   t_start_ms=base, duration_ms=45000)
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-0",
                   a_number="4477001234", b_number="0099900001234",
                   t_start_ms=base + 600, duration_ms=45300, source_ip="10.0.0.1")

    # Exact match fails (different full numbers) -> 0 % completion.
    exact = LoopMatcher(db=db).match_campaign(CID, match_key="exact")
    assert exact["calls_in_matched"] == 0
    assert exact["completion_pct"] == 0.0
    # The unmatched inbound is NOT attributed to this campaign (matched legs only).
    assert exact["minutes_in_ms"] == 0

    # suffix6 keys on the shared trailing 001234 -> closes the loop.
    suff = LoopMatcher(db=db).match_campaign(CID, match_key="suffix6")
    assert suff["calls_in_matched"] == 1
    assert suff["completion_pct"] == 100.0
    assert suff["delta_avg_ms"] == 300.0


# ── window edge handling ────────────────────────────────────────────────────


def test_window_edge_inside_and_outside(db):
    """An inbound just inside the window matches; one just outside does not.

    minutes_in is summed over MATCHED legs only — the answer side is shared
    across campaigns and inbound carries no campaign id, so only the legs paired
    to this campaign's outbound count. The inside inbound matches (and counts);
    the outside one neither matches nor counts."""
    base = 1_700_000_000_000
    window_s = 60  # tight 60 s window for the test
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="out-in",
                   a_number="1000", b_number="700000001",
                   t_start_ms=base, duration_ms=20000)
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="out-out",
                   a_number="1000", b_number="700000002",
                   t_start_ms=base, duration_ms=20000)
    # Just inside: +59 s.
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-inside",
                   a_number="700000001", b_number="700000001",
                   t_start_ms=base + 59_000, duration_ms=20100)
    # Just outside: +61 s (beyond the campaign window).
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-outside",
                   a_number="700000002", b_number="700000002",
                   t_start_ms=base + 61_000, duration_ms=20200)

    stats = LoopMatcher(db=db).match_campaign(CID, match_key="exact",
                                              window_s=window_s)
    assert stats["calls_in_matched"] == 1  # only the inside one
    assert stats["completion_pct"] == 50.0
    # minutes_in counts the matched leg only; the +61 s one never matched.
    assert stats["minutes_in_ms"] == 20100


def test_default_window_constant():
    # Sanity: the default join window is 1 h per the spec (§4.3).
    assert DEFAULT_WINDOW_S == 3600


# ── unmatched inbound is excluded from a campaign's minutes ─────────────────


def test_unmatched_inbound_not_counted(db):
    """An inbound call with NO matching outbound is NOT added to minutes_in. The
    answer side is shared across campaigns and inbound carries no campaign id, so
    only matched legs are attributed to THIS campaign — otherwise every concurrent
    campaign sums the same shared inbound (the cy213 ~3x per-campaign over-count,
    where global in≈out but each campaign reported ~3x its outbound)."""
    base = 1_700_000_000_000
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="out-0",
                   a_number="1000", b_number="800000001",
                   t_start_ms=base, duration_ms=10000)
    # Returns the matching one ...
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-0",
                   a_number="800000001", b_number="800000001",
                   t_start_ms=base + 300, duration_ms=10200)
    # ... plus a stray inbound for a number we never dialed (e.g. another
    # campaign's returning leg landing on the shared answer port).
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-stray",
                   a_number="999999999", b_number="999999999",
                   t_start_ms=base + 300, duration_ms=5000)

    stats = LoopMatcher(db=db).match_campaign(CID, match_key="exact")
    assert stats["calls_in_matched"] == 1
    assert stats["completion_pct"] == 100.0
    # minutes_in counts ONLY the matched leg; the stray inbound is excluded.
    assert stats["minutes_in_ms"] == 10200


# ── failures by SIP code (out & in separately) + histogram ──────────────────


def test_failures_by_code_separated(db):
    """Failed calls are tallied by SIP code per direction, and a non-2xx
    outbound is excluded from answered_out / completion."""
    base = 1_700_000_000_000
    # One good outbound that returns.
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="out-ok",
                   a_number="1000", b_number="900000001",
                   t_start_ms=base, duration_ms=30000)
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-ok",
                   a_number="900000001", b_number="900000001",
                   t_start_ms=base + 300, duration_ms=30200)
    # Outbound failures: 487 and 503.
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="out-487",
                   a_number="1000", b_number="900000002",
                   t_start_ms=base, duration_ms=0, final_code=487)
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="out-503",
                   a_number="1000", b_number="900000003",
                   t_start_ms=base, duration_ms=0, final_code=503)
    # Inbound failure: 408.
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-408",
                   a_number="900000009", b_number="900000009",
                   t_start_ms=base, duration_ms=0, final_code=408)

    stats = LoopMatcher(db=db).match_campaign(CID, match_key="exact")
    assert stats["calls_out"] == 3
    assert stats["answered_out"] == 1          # the two failures excluded
    assert stats["completion_pct"] == 100.0    # 1 matched / 1 answered
    assert stats["failures"]["out"] == {"487": 1, "503": 1}
    assert stats["failures"]["in"] == {"408": 1}
    # Histogram is present and totals the matched-pair count (1 delta).
    total = sum(b["count"] for b in stats["delta_histogram"])
    assert total == 1


# ── latest_stats round-trip (the GET endpoint reads this) ───────────────────


def test_latest_stats_roundtrip(db):
    """After a pass, latest_stats() returns the persisted snapshot with the
    failures/histogram/unmatched blocks re-expanded from failures_json."""
    base = 1_700_000_000_000
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="out-0",
                   a_number="1000", b_number="110000001",
                   t_start_ms=base, duration_ms=12000)
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-0",
                   a_number="110000001", b_number="110000001",
                   t_start_ms=base + 300, duration_ms=12150)
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="out-fail",
                   a_number="1000", b_number="110000002",
                   t_start_ms=base, duration_ms=0, final_code=404)

    matcher = LoopMatcher(db=db)
    live = matcher.match_campaign(CID, match_key="exact")

    latest = matcher.latest_stats(CID)
    assert latest is not None
    assert latest["calls_out"] == live["calls_out"]
    assert latest["completion_pct"] == live["completion_pct"]
    assert latest["minutes_in_ms"] == 12150
    assert latest["failures"]["out"] == {"404": 1}
    assert isinstance(latest["delta_histogram"], list)
    assert latest["unmatched_pairs"] == []


def test_latest_stats_none_before_any_pass(db):
    assert LoopMatcher(db=db).latest_stats("never-ran") is None


def test_matched_record_id_stamped(db):
    """The matcher stamps matched_record_id on both sides of each pair."""
    from sqlalchemy import text

    base = 1_700_000_000_000
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="out-0",
                   a_number="1000", b_number="120000001",
                   t_start_ms=base, duration_ms=15000)
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-0",
                   a_number="120000001", b_number="120000001",
                   t_start_ms=base + 300, duration_ms=15200)

    LoopMatcher(db=db).match_campaign(CID, match_key="exact")
    with db.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT call_uuid, matched_record_id FROM call_records "
                 "ORDER BY id")
        ).fetchall()
    by_uuid = {r[0]: r[1] for r in rows}
    # Both records now point at each other (non-null).
    assert by_uuid["out-0"] is not None
    assert by_uuid["in-0"] is not None


def test_no_db_is_inert():
    """With no DB the matcher returns an all-zero stats dict and never raises."""
    stats = LoopMatcher(db=None).match_campaign("x", match_key="exact")
    assert stats["calls_out"] == 0
    assert stats["minutes_in_ms"] == 0
    assert stats["completion_pct"] == 0.0


# ── concurrent campaigns must not double-count global inbound minutes ────────


def test_concurrent_campaigns_do_not_double_count_inbound(db):
    """Two campaigns running in DIFFERENT time windows must each see only the
    inbound minutes inside THEIR OWN window — not the global inbound sum.

    Regression for the §4.3 N-times inflation bug: inbound carries no campaign
    id, so loading ALL inbound for every campaign made each campaign sum the
    same global inbound minutes. With inbound scoped to the campaign's join
    window, campaign A (early window) and campaign B (a day later) each count
    only their own returning leg.
    """
    window_s = 3600  # 1 h default
    a_base = 1_700_000_000_000
    b_base = a_base + 24 * 3600 * 1000  # +24 h: far outside A's 1 h window

    # Campaign A: one outbound + its returning inbound, ~0.5 s later.
    _insert_record(db, campaign_id="camp-A", direction="out", call_uuid="a-out",
                   a_number="1000", b_number="300000001",
                   t_start_ms=a_base, duration_ms=60000)
    _insert_record(db, campaign_id=None, direction="in", call_uuid="a-in",
                   a_number="300000001", b_number="300000001",
                   t_start_ms=a_base + 500, duration_ms=60200, source_ip="10.0.0.1")

    # Campaign B: a DIFFERENT outbound + its inbound, 24 h later.
    _insert_record(db, campaign_id="camp-B", direction="out", call_uuid="b-out",
                   a_number="1000", b_number="300000002",
                   t_start_ms=b_base, duration_ms=30000)
    _insert_record(db, campaign_id=None, direction="in", call_uuid="b-in",
                   a_number="300000002", b_number="300000002",
                   t_start_ms=b_base + 500, duration_ms=30300, source_ip="10.0.0.1")

    matcher = LoopMatcher(db=db)
    a_stats = matcher.match_campaign("camp-A", match_key="exact", window_s=window_s)
    b_stats = matcher.match_campaign("camp-B", match_key="exact", window_s=window_s)

    # Each campaign sees ONLY its own inbound minutes — NOT the global sum
    # (60200 + 30300). Before the fix both would report the combined 90500.
    assert a_stats["minutes_in_ms"] == 60200
    assert b_stats["minutes_in_ms"] == 30300
    assert a_stats["calls_in_matched"] == 1
    assert b_stats["calls_in_matched"] == 1
    assert a_stats["completion_pct"] == 100.0
    assert b_stats["completion_pct"] == 100.0


# ── bounded incremental matching (memory-leak fix) ───────────────────────────


def test_incremental_matching_is_cumulative_and_idempotent(db):
    """The bounded matcher pairs only the still-UNMATCHED rows each pass (so it
    never reloads a campaign's whole history — the GB leak), yet keeps the
    cumulative totals lifetime-accurate and never re-pairs a matched call."""
    m = LoopMatcher(db=db)

    # Call 1 → pass 1: one matched pair, cumulative = 1.
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="o1",
                   a_number="100", b_number="22460111", t_start_ms=1000,
                   duration_ms=60000)
    _insert_record(db, campaign_id=None, direction="in", call_uuid="i1",
                   a_number="100", b_number="22460111", t_start_ms=1200,
                   duration_ms=60000)
    s1 = m.match_campaign(CID, match_key="exact")
    assert s1["calls_out"] == 1
    assert s1["calls_in_matched"] == 1
    assert s1["minutes_in_ms"] == 60000
    assert s1["completion_pct"] == 100.0

    # Pass again with NO new data: nothing re-paired, cumulative unchanged.
    s1b = m.match_campaign(CID, match_key="exact")
    assert s1b["calls_in_matched"] == 1
    assert s1b["minutes_in_ms"] == 60000

    # Call 2 added → next pass: cumulative grows to 2 (call 1 not double-counted).
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="o2",
                   a_number="101", b_number="22460222", t_start_ms=2000,
                   duration_ms=30000)
    _insert_record(db, campaign_id=None, direction="in", call_uuid="i2",
                   a_number="101", b_number="22460222", t_start_ms=2200,
                   duration_ms=30000)
    s2 = m.match_campaign(CID, match_key="exact")
    assert s2["calls_out"] == 2
    assert s2["calls_in_matched"] == 2
    assert s2["minutes_in_ms"] == 90000

    # Pairing is durable on both legs (2 pairs x 2 legs = 4 stamped rows).
    from sqlalchemy import text
    with db.engine.connect() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM call_records "
                 "WHERE matched_record_id IS NOT NULL")
        ).fetchone()[0]
    assert n == 4


# ── daily target: answered-outbound minutes since 00:00 GMT (resets each day) ─


def test_minutes_out_today_ms_windows_to_gmt_day(db):
    """The daily target measures answered-outbound minutes since 00:00 GMT today;
    earlier GMT days are excluded, so the target bar resets at GMT midnight."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    today = now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    two_days_ago = (now - _dt.timedelta(days=2)).isoformat()
    # Two answered-outbound today: 120 s + 60 s = 180000 ms.
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="t1",
                   a_number="1", b_number="222001", t_start_ms=1000,
                   duration_ms=120000, created_at=today)
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="t2",
                   a_number="1", b_number="222002", t_start_ms=2000,
                   duration_ms=60000, created_at=today)
    # Answered-outbound two days ago — excluded by the GMT-day window.
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="old",
                   a_number="1", b_number="222003", t_start_ms=3000,
                   duration_ms=999000, created_at=two_days_ago)
    # A non-2xx today — never "answered", so excluded.
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="fail",
                   a_number="1", b_number="222004", t_start_ms=4000,
                   duration_ms=0, final_code=503, created_at=today)
    assert LoopMatcher(db=db).minutes_out_today_ms(CID) == 180000


def test_minutes_out_today_ms_no_db_is_zero():
    assert LoopMatcher(db=None).minutes_out_today_ms("x") == 0


def test_latest_stats_includes_minutes_out_today(db):
    """latest_stats() (what GET /api/loops/{id} reads) carries the live daily
    figure the target bar measures against."""
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).replace(
        hour=10, minute=0, second=0, microsecond=0).isoformat()
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="o1",
                   a_number="1", b_number="333001", t_start_ms=1000,
                   duration_ms=45000, created_at=today)
    m = LoopMatcher(db=db)
    m.match_campaign(CID, match_key="exact")
    latest = m.latest_stats(CID)
    assert latest is not None
    assert latest["minutes_out_today_ms"] == 45000

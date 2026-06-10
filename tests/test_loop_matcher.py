"""
LoopMatcher tests (design §4.3 / §5, build stage "Matcher").

Covers, with no real SIPp/Docker/Linux (SQLite only — synthetic call_records
inserted directly), the spec's required matcher behaviors:

  * correct completion_pct (matched inbound / answered outbound),
  * minutes out (A-side) and minutes in (B-side, ALL inbound — never understated),
  * per-call delta avg / p50 / p95 + the small histogram,
  * suffix matching when the chain rewrites leading digits,
  * window edge handling (just-inside vs just-outside the join window),
  * unmatched-inbound counting (its minutes still count toward minutes_in),
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
                   source_ip=None):
    """Insert one synthetic call_record. t_answer/t_end are derived so the
    neutral duration math (t_end - t_answer) reproduces ``duration_ms``."""
    from sqlalchemy import text

    t_answer = t_start_ms + 50 if final_code and 200 <= final_code < 300 else None
    t_end = (t_answer + duration_ms) if t_answer is not None else None
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
                "created_at": "2026-06-10T00:00:00+00:00",
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
    # But minutes_in still counts the unmatched inbound call (never understated).
    assert exact["minutes_in_ms"] == 45300

    # suffix6 keys on the shared trailing 001234 -> closes the loop.
    suff = LoopMatcher(db=db).match_campaign(CID, match_key="suffix6")
    assert suff["calls_in_matched"] == 1
    assert suff["completion_pct"] == 100.0
    assert suff["delta_avg_ms"] == 300.0


# ── window edge handling ────────────────────────────────────────────────────


def test_window_edge_inside_and_outside(db):
    """An inbound just inside the window matches; one just outside does not —
    but its minutes still count toward minutes_in."""
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
    # Just outside: +61 s.
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-outside",
                   a_number="700000002", b_number="700000002",
                   t_start_ms=base + 61_000, duration_ms=20200)

    stats = LoopMatcher(db=db).match_campaign(CID, match_key="exact",
                                              window_s=window_s)
    assert stats["calls_in_matched"] == 1  # only the inside one
    assert stats["completion_pct"] == 50.0
    # Both inbound minutes count regardless of the match outcome.
    assert stats["minutes_in_ms"] == 20100 + 20200


def test_default_window_constant():
    # Sanity: the default join window is 1 h per the spec (§4.3).
    assert DEFAULT_WINDOW_S == 3600


# ── unmatched inbound counting (minutes never understated) ──────────────────


def test_unmatched_inbound_minutes_counted(db):
    """An inbound call with NO matching outbound still adds to minutes_in but
    not to calls_in_matched or completion."""
    base = 1_700_000_000_000
    _insert_record(db, campaign_id=CID, direction="out", call_uuid="out-0",
                   a_number="1000", b_number="800000001",
                   t_start_ms=base, duration_ms=10000)
    # Returns the matching one ...
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-0",
                   a_number="800000001", b_number="800000001",
                   t_start_ms=base + 300, duration_ms=10200)
    # ... plus a stray inbound for a number we never dialed.
    _insert_record(db, campaign_id=None, direction="in", call_uuid="in-stray",
                   a_number="999999999", b_number="999999999",
                   t_start_ms=base + 300, duration_ms=5000)

    stats = LoopMatcher(db=db).match_campaign(CID, match_key="exact")
    assert stats["calls_in_matched"] == 1
    assert stats["completion_pct"] == 100.0
    # minutes_in includes BOTH the matched and the stray inbound call.
    assert stats["minutes_in_ms"] == 10200 + 5000


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

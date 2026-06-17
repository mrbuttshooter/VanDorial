"""
Adaptive number-pool optimizer tests (gencall/core/pool_optimizer.py).

Locks in the policy that cuts 404 "no route" on loops: learn which destination
prefixes route from call_records, keep them, prune the 404-heavy ones, and
rebuild the pool to only the routable prefixes.
"""

import pytest
from sqlalchemy import text

from gencall.db.models import Database
from gencall.db.migrations import apply_migrations
from gencall.core import pool_optimizer as po


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'opt.db'}")
    database.create_tables()
    apply_migrations(database.engine)
    return database


def _seed(db, cid, b_number, final_code, answered):
    """Insert one call_record. answered => a billed duration + 2xx."""
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO call_records (campaign_id, direction, call_uuid, "
                "a_number, b_number, t_start_ms, t_answer_ms, t_end_ms, "
                "duration_ms, final_code, created_at) VALUES "
                "(:cid,'out',:u,'353100000000',:b,1000,:ans,:end,:dur,:fc,'2026-06-17T00:00:00Z')"
            ),
            {"cid": cid, "u": f"{b_number}-{final_code}-{id(object())}",
             "b": b_number, "ans": 1120 if answered else None,
             "end": 91120 if answered else None,
             "dur": 90000 if answered else 0, "fc": final_code},
        )


def test_prefix_asr_groups_and_counts(db):
    cid = "camp-1"
    # 224626x -> all answered (routable); 224620x -> all 404 (dead).
    for i in range(5):
        _seed(db, cid, f"224626{i:06d}", 200, True)
    for i in range(4):
        _seed(db, cid, f"224620{i:06d}", 404, False)
    stats = po.prefix_asr(db, cid, prefix_len=6)
    assert stats["224626"] == [5, 5]
    assert stats["224620"] == [0, 4]


def test_classify_keeps_routable_drops_dead_spares_thin(db):
    stats = {
        "224626": [18, 20],   # 90% over 20 -> keep
        "224620": [1, 25],    # 4%  over 25 -> drop
        "224629": [2, 3],     # only 3 attempts -> undecided (spared)
    }
    keep, drop, undecided = po.classify_prefixes(stats, min_attempts=10, min_asr=0.5)
    assert keep == ["224626"]
    assert drop == ["224620"]
    assert undecided == ["224629"]


def test_optimize_rebuilds_pool_excluding_dead_prefix(db, tmp_path):
    cid = "camp-2"
    # routable block (answered) + a dead block (404), enough attempts to judge.
    for i in range(30):
        _seed(db, cid, f"224626{i:06d}", 200, True)
    for i in range(30):
        _seed(db, cid, f"224620{i:06d}", 404, False)

    campaign = {"id": cid, "node_id": None, "pool_count": 200}
    node = {"origin_zone": "", "origin_code": "353", "pool_count": 200, "pool_length": 12}
    report = po.optimize(db, campaign, node, prefix_len=6, min_attempts=10, min_asr=0.5)

    assert report is not None, "a proven-dead prefix should trigger a rebuild"
    assert "224620" in report["dropped"]
    assert "224626" in report["kept"]
    # Every generated B-number must be in a kept prefix, never the dead one.
    with open(report["csv_path"]) as fh:
        bnums = [ln.split(";")[1] for ln in fh.read().splitlines() if ";" in ln]
    assert bnums, "pool must not be empty"
    assert all(not b.startswith("224620") for b in bnums)
    assert all(b.startswith("224626") for b in bnums)


def test_optimize_noop_when_nothing_proven_dead(db):
    cid = "camp-3"
    # Only a small, all-answered sample -> no dead prefix -> leave pool alone.
    for i in range(5):
        _seed(db, cid, f"224626{i:06d}", 200, True)
    report = po.optimize(db, {"id": cid, "node_id": None}, {"origin_code": "353"},
                         min_attempts=10, min_asr=0.5)
    assert report is None


def test_optimize_noop_when_all_dead(db):
    cid = "camp-4"
    for i in range(30):
        _seed(db, cid, f"224620{i:06d}", 404, False)
    # Everything with enough data is dead -> don't rebuild to an empty pool.
    report = po.optimize(db, {"id": cid, "node_id": None}, {"origin_code": "353"},
                         min_attempts=10, min_asr=0.5)
    assert report is None

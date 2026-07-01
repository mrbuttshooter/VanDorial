"""Regression tests for the Wave 3 bug-hunt fixes (improve/gencall-3.0-loop).

All non-call-path: Postgres BOOLEAN-default portability (the migration wedge),
the fleet completion_pct denominator, and the adaptive-pool origin-zone resolver.
"""

import os

import pytest


# ─── #1/#4 BOOLEAN DEFAULT 0/1 must be Postgres-portable ───────────────────────


def test_bool_default_translated_for_postgres():
    from gencall.db.migrations import _translate_for_dialect
    s = "ALTER TABLE t ADD COLUMN flag BOOLEAN DEFAULT 0"
    assert "DEFAULT FALSE" in _translate_for_dialect(s, "postgresql")
    assert "DEFAULT TRUE" in _translate_for_dialect(
        "ALTER TABLE t ADD COLUMN flag BOOLEAN DEFAULT 1", "postgresql")
    # SQLite is left untouched (it accepts 0/1).
    assert _translate_for_dialect(s, "sqlite") == s


def test_migration_0007_uses_boolean_false():
    """The shipped 0007 must not carry the raw `BOOLEAN DEFAULT 0` that Postgres
    rejects (which wedged the whole migration chain on prod Postgres boxes)."""
    from gencall.db import migrations
    path = os.path.join(os.path.dirname(migrations.__file__),
                        "0007_loop_campaign_profile.sql")
    with open(path, encoding="utf-8") as fh:
        sql = fh.read()
    assert "BOOLEAN DEFAULT FALSE" in sql
    assert "BOOLEAN DEFAULT 0" not in sql


def test_models_added_columns_have_no_bare_boolean_zero():
    from gencall.db.models import Database
    for table, cols in Database._ADDED_COLUMNS.items():
        for col, ddl in cols:
            assert ddl.strip() != "BOOLEAN DEFAULT 0", f"{table}.{col} is not PG-portable"


# ─── #2 fleet completion_pct denominator = answered_out ────────────────────────


def test_aggregate_completion_pct_uses_answered_out():
    from gencall.controller.aggregator import aggregate_loop_stats
    per_node = {
        1: {"calls_out": 100, "answered_out": 90, "minutes_out_ms": 0,
            "calls_in_matched": 80, "minutes_in_ms": 0},
        2: {"calls_out": 40, "answered_out": 38, "minutes_out_ms": 0,
            "calls_in_matched": 30, "minutes_in_ms": 0},
    }
    agg = aggregate_loop_stats(per_node)
    assert agg["answered_out"] == 128
    # matched-in / answered-out, NOT / calls_out (140).
    assert agg["completion_pct"] == round(110 / 128 * 100, 2)


def test_aggregate_completion_pct_zero_answered_guarded():
    from gencall.controller.aggregator import aggregate_loop_stats
    agg = aggregate_loop_stats({
        1: {"calls_out": 10, "answered_out": 0, "minutes_out_ms": 0,
            "calls_in_matched": 0, "minutes_in_ms": 0}})
    assert agg["completion_pct"] == 0.0


# ─── #3 adaptive rebuild resolves a fuzzy / overlay origin zone ────────────────


def test_rebuild_pool_csv_resolves_case_insensitive_origin_zone(tmp_path):
    """rebuild_pool_csv must resolve the origin like creation does (find_zone),
    not an exact dict-key lookup — otherwise the adaptive optimizer permanently
    no-ops for a non-exact/overlay origin zone."""
    from gencall.core.pool_optimizer import rebuild_pool_csv
    # "afghanistan" (lowercase) is NOT an exact deck key ("Afghanistan" is), so
    # the old zones.get() lookup raised; find_zone resolves it case-insensitively.
    path, rows = rebuild_pool_csv(
        origin_zone="afghanistan", origin_code="",
        keep_prefixes=["224626"], count=5, out_dir=str(tmp_path))
    assert rows == 5
    assert os.path.isfile(path)


def test_rebuild_pool_csv_still_raises_for_unknown_zone(tmp_path):
    from gencall.core.pool_optimizer import rebuild_pool_csv
    with pytest.raises(ValueError):
        rebuild_pool_csv(
            origin_zone="not-a-real-zone-xyz", origin_code="",
            keep_prefixes=["224626"], count=5, out_dir=str(tmp_path))

"""Regression tests for the Wave 1 bug-hunt fixes (improve/gencall-3.0-loop).

Each test pins one confirmed defect so it can't silently regress. Every fix here
is non-call-path (the SIP/INVITE/media generation is untouched): config parsing,
the Postgres DSN builder, the fleet rate split, the API rate limiter, the WS-auth
DB-write, and the migration runner's add-column tolerance.
"""


import pytest

from gencall.core.config import Config


# ─── #2 ConfigParser interpolation crash on a literal '%' ──────────────────────


def _write_cfg(tmp_path, body: str) -> str:
    p = tmp_path / "gencall.cfg"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_config_returns_percent_values_verbatim(tmp_path):
    """A literal '%' in a config value must be returned as-is, not treated as an
    interpolation token (which used to raise InterpolationSyntaxError at first
    access and crash boot)."""
    cfg = _write_cfg(tmp_path, "[fleet]\ntoken = ab%cd%ef\n")
    Config.reset()
    try:
        c = Config(path=cfg)
        assert c.fleet_token == "ab%cd%ef"
    finally:
        Config.reset()


def test_config_pg_password_with_percent_does_not_crash(tmp_path, monkeypatch):
    """A '%' in pg_password (no env override) must build the DSN instead of
    raising — and the '%' must be percent-encoded in the URL."""
    for var in ("GENCALL_DATABASE_URL", "GENCALL_DB_ENGINE", "GENCALL_PG_USER",
                "GENCALL_PG_PASSWORD", "GENCALL_PG_HOST", "GENCALL_PG_PORT",
                "GENCALL_PG_DATABASE"):
        monkeypatch.delenv(var, raising=False)
    cfg = _write_cfg(
        tmp_path,
        "[database]\nengine = postgresql\npg_user = gencall\n"
        "pg_password = p%ssw0rd\npg_host = db.local\npg_port = 5432\n"
        "pg_database = gencall\n",
    )
    Config.reset()
    try:
        url = Config(path=cfg).db_url  # must not raise
        assert url.startswith("postgresql://gencall:")
        assert "p%25ssw0rd" in url          # '%' encoded, not literal
        assert "@db.local:5432/gencall" in url
    finally:
        Config.reset()


# ─── #3 Unencoded DB credentials corrupt the DSN ───────────────────────────────


def test_config_pg_dsn_percent_encodes_special_chars(monkeypatch):
    """Credentials with @ : / that would otherwise redirect the connection are
    percent-encoded (env-provided path)."""
    monkeypatch.delenv("GENCALL_DATABASE_URL", raising=False)
    monkeypatch.setenv("GENCALL_DB_ENGINE", "postgresql")
    monkeypatch.setenv("GENCALL_PG_USER", "user@corp")
    monkeypatch.setenv("GENCALL_PG_PASSWORD", "p@ss:w/rd")
    monkeypatch.setenv("GENCALL_PG_HOST", "db.internal")
    monkeypatch.setenv("GENCALL_PG_PORT", "6543")
    monkeypatch.setenv("GENCALL_PG_DATABASE", "main")
    Config.reset()
    try:
        url = Config().db_url
        # The single authority '@' separating creds from host is the encoded
        # one's neighbour: creds must contain no raw '@' or ':' beyond the
        # user:pass separator, so the host parses correctly.
        assert url == "postgresql://user%40corp:p%40ss%3Aw%2Frd@db.internal:6543/main"
    finally:
        Config.reset()
        for var in ("GENCALL_DB_ENGINE", "GENCALL_PG_USER", "GENCALL_PG_PASSWORD",
                    "GENCALL_PG_HOST", "GENCALL_PG_PORT", "GENCALL_PG_DATABASE"):
            monkeypatch.delenv(var, raising=False)


# ─── #10 split_rate('total', ...) must reject un-splittable totals ─────────────


def test_split_rate_total_too_small_raises():
    from gencall.controller.aggregator import split_rate
    # 0.02 cps across 3 nodes -> 2 hundredth-units for 3 nodes: some node gets 0.
    with pytest.raises(ValueError):
        split_rate("total", 0.02, 3)


def test_split_rate_total_zero_raises():
    from gencall.controller.aggregator import split_rate
    with pytest.raises(ValueError):
        split_rate("total", 0.0, 2)


def test_split_rate_total_valid_still_splits():
    from gencall.controller.aggregator import split_rate
    # Regression guard: a valid split is unchanged by the new bounds check.
    assert split_rate("total", 9.0, 3) == [3.0, 3.0, 3.0]
    assert split_rate("per_node", 5.0, 3) == [5.0, 5.0, 5.0]
    assert split_rate("total", 10.0, 0) == []


# ─── #11 RateLimiter must enforce limits above the old maxlen=1000 ─────────────


def test_rate_limiter_enforces_limit_above_1000():
    from gencall.core.api_gateway import RateLimiter
    rl = RateLimiter()
    limit = 1500
    # The first `limit` requests are allowed; the next is denied. With the old
    # deque(maxlen=1000) the bucket could never reach 1500 so this never denied.
    for _ in range(limit):
        assert rl.check("k", limit) is True
    assert rl.check("k", limit) is False


def test_rate_limiter_bucket_is_unbounded():
    from gencall.core.api_gateway import RateLimiter
    rl = RateLimiter()
    rl.check("k", 5000)
    assert rl._buckets["k"].maxlen is None


# ─── #12 validate_key(touch=False) must not record usage ──────────────────────


def test_validate_key_touch_false_does_not_increment():
    from gencall.core.api_gateway import APIKeyManager
    mgr = APIKeyManager(db=None)
    raw, key = mgr.create_key("ws-test")
    assert key.request_count == 0
    assert mgr.validate_key(raw, touch=False) is not None
    assert key.request_count == 0          # read-only: unchanged
    assert mgr.validate_key(raw) is not None  # default still counts
    assert key.request_count == 1


# ─── #8 Migration runner tolerates an already-present ADD COLUMN ──────────────


def test_migration_rerun_tolerates_preexisting_add_column(tmp_path):
    """End-to-end: if an ADD COLUMN migration re-runs against a column that now
    already exists (the real wedge — ensure_added_columns/an earlier partial run
    beat it), the runner must skip that statement and re-record the file instead
    of raising 'duplicate column' and wedging every later migration forever."""
    from sqlalchemy import text
    from gencall.db.migrations import apply_migrations
    from gencall.db.models import Database

    db = Database(f"sqlite:///{tmp_path / 'wedge.db'}")
    db.create_tables()
    apply_migrations(db.engine)  # first pass: local_ip/profile columns now exist

    # Force the add-column files to re-run while their columns already exist.
    readd = ["0006_loop_campaign_local_ip.sql", "0007_loop_campaign_profile.sql"]
    with db.engine.begin() as conn:
        for f in readd:
            conn.execute(
                text("DELETE FROM schema_migrations WHERE filename = :f"), {"f": f}
            )

    # Before the fix this raised OperationalError('duplicate column name'); now
    # the pre-existing ADD COLUMNs are skipped and the files are re-recorded.
    applied = apply_migrations(db.engine)
    assert set(readd) <= set(applied)

    with db.engine.begin() as conn:
        done = {r[0] for r in conn.execute(text("SELECT filename FROM schema_migrations"))}
    assert set(readd) <= done

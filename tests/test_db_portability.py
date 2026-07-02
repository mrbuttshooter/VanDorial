"""
DB-portability tests (production is PostgreSQL; the test suite runs on SQLite).

These pin the bugs that SQLite-only testing hid and that aborted/silently broke
the records/stats schema on the real PostgreSQL box:

  * a Postgres-syntax lint over the migration + query SQL: no bare
    ``AUTOINCREMENT`` and no SQLite-only ``IS :param`` NULL comparison may reach
    PostgreSQL — both abort there;
  * the migration runner's dialect translation: ``INTEGER PRIMARY KEY
    AUTOINCREMENT`` (SQLite) becomes ``BIGSERIAL PRIMARY KEY`` on postgresql and
    is left untouched on sqlite.

No real PostgreSQL is required: the lint reads the SQL text, and the translation
is exercised directly against the runner's pure helper.
"""

import os
import re


MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "gencall", "db", "migrations",
)
CORE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "gencall", "core",
)


def _migration_sql_files():
    return [
        os.path.join(MIGRATIONS_DIR, f)
        for f in os.listdir(MIGRATIONS_DIR)
        if f.endswith(".sql")
    ]


# ── Postgres-syntax lint ─────────────────────────────────────────────────────


def test_migrations_have_no_raw_autoincrement_for_postgres():
    """After dialect translation, no statement bound for PostgreSQL may carry
    the SQLite-only AUTOINCREMENT keyword (it is a hard syntax error there)."""
    from gencall.db.migrations import (
        _split_statements,
        _translate_for_dialect,
    )

    for path in _migration_sql_files():
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()
        for stmt in _split_statements(sql):
            pg = _translate_for_dialect(stmt, "postgresql")
            assert "AUTOINCREMENT" not in pg.upper(), (
                f"{os.path.basename(path)}: AUTOINCREMENT survives translation "
                f"to PostgreSQL -> {pg!r}"
            )


def test_query_sql_has_no_sqlite_only_is_param():
    """No core module may use the SQLite-only ``IS :param`` NULL comparison.

    ``WHERE col IS :param`` is valid on SQLite but a syntax error on PostgreSQL;
    the NULL-safe portable form is ``IS NOT DISTINCT FROM :param``. Scan the
    core modules' raw SQL for the offending pattern (``IS :name`` not followed
    by ``NOT DISTINCT FROM``).
    """
    bad = []
    for fname in os.listdir(CORE_DIR):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(CORE_DIR, fname)
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        for lineno, line in enumerate(lines, 1):
            # Strip a Python line comment so the pattern in THIS test's own
            # docstrings/comments (and explanatory comments in the source) is not
            # falsely flagged — we only care about actual SQL strings.
            code = line.split("#", 1)[0]
            for m in re.finditer(r"\bIS\s+:\w+", code, re.IGNORECASE):
                snippet = code[max(0, m.start() - 30): m.end()]
                if "NOT DISTINCT FROM" not in snippet.upper():
                    bad.append(f"{fname}:{lineno}: ...{snippet.strip()}...")
    assert not bad, "SQLite-only 'IS :param' found (use IS NOT DISTINCT FROM): " + \
        "; ".join(bad)


# ── migration dialect translation ────────────────────────────────────────────


def test_autoincrement_translates_to_bigserial_on_postgres():
    from gencall.db.migrations import _translate_for_dialect

    stmt = "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, x VARCHAR(8))"
    pg = _translate_for_dialect(stmt, "postgresql")
    assert "BIGSERIAL PRIMARY KEY" in pg
    assert "AUTOINCREMENT" not in pg.upper()
    # Whitespace-tolerant: extra spaces between the keywords still match.
    spaced = "id   INTEGER   PRIMARY  KEY   AUTOINCREMENT"
    assert "BIGSERIAL PRIMARY KEY" in _translate_for_dialect(spaced, "postgresql")


def test_autoincrement_left_intact_on_sqlite():
    from gencall.db.migrations import _translate_for_dialect

    stmt = "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)"
    assert _translate_for_dialect(stmt, "sqlite") == stmt


def test_migrations_apply_cleanly_under_sqlite(tmp_path):
    """The full migration set still applies on SQLite (the untouched path)."""
    from gencall.db.migrations import apply_migrations
    from gencall.db.models import Database

    db = Database(f"sqlite:///{tmp_path / 'port.db'}")
    db.create_tables()
    applied = apply_migrations(db.engine)
    # Every NNNN_*.sql migration is applied on a fresh DB.
    assert len(applied) == len(_migration_sql_files())

    # The records/stats tables the parser/matcher depend on now exist.
    from sqlalchemy import inspect

    names = set(inspect(db.engine).get_table_names())
    assert {"call_records", "loop_stats", "retention_runs"} <= names

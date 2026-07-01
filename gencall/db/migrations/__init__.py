"""
Plain ordered SQL migrations for GenCall.

No Alembic dependency — the schema is small and the deploy target is a single
4 GB box, so a flat list of ``NNNN_name.sql`` files applied in lexical order
(tracked in a ``schema_migrations`` table) is enough. Each file is executed once;
already-applied files are skipped. Statements are split on ``;`` and run in a
single transaction per file so a half-applied migration never lands.

This is intentionally not an ORM concern: ``managed_processes`` (design §4.5) is
written/read with raw SQL from ``process_registry`` so the reliability layer does
not depend on the SQLAlchemy models being importable during reconciliation.
"""

import logging
import os

logger = logging.getLogger("gencall.db.migrations")

# Directory holding the ordered .sql files (this package's own directory).
MIGRATIONS_DIR = os.path.dirname(__file__)


def _list_migration_files():
    """Return migration filenames (NNNN_*.sql) in applied (lexical) order."""
    files = [
        f
        for f in os.listdir(MIGRATIONS_DIR)
        if f.endswith(".sql") and f[:4].isdigit()
    ]
    return sorted(files)


def _strip_comments(sql_text):
    """Remove ``--`` comments (full-line and inline) so a ';' inside a comment
    never splits a statement. String literals in our migrations contain no
    ``--``, so a simple per-line strip at the first ``--`` is safe."""
    out_lines = []
    for line in sql_text.splitlines():
        idx = line.find("--")
        if idx != -1:
            line = line[:idx]
        out_lines.append(line)
    return "\n".join(out_lines)


def _split_statements(sql_text):
    """Split a .sql file into individual statements on ';'.

    The migration files use only simple statements (no procedural bodies with
    embedded semicolons), so a plain split is safe and keeps us dependency-free.
    Comments are stripped first so a ';' inside a comment cannot break a
    statement; blank fragments are dropped.
    """
    cleaned = _strip_comments(sql_text)
    statements = []
    for chunk in cleaned.split(";"):
        stmt = chunk.strip()
        if stmt:
            statements.append(stmt)
    return statements


# ── dialect translation ──────────────────────────────────────────────────────
#
# The migration files are authored with SQLite syntax (the test DB), but
# production is PostgreSQL. ``INTEGER PRIMARY KEY AUTOINCREMENT`` is a
# SQLite-ONLY construct: on PostgreSQL it is a hard syntax error that aborts the
# whole migration transaction, so the records/stats schema would never be
# created on the box. We rewrite it per dialect at apply time, branching on the
# engine's dialect name (``engine.dialect.name``):
#
#   * sqlite      -> kept as-is (``INTEGER PRIMARY KEY AUTOINCREMENT``)
#   * postgresql  -> ``BIGSERIAL PRIMARY KEY`` (auto-incrementing 64-bit id)
#   * other       -> kept as-is (best-effort; sqlite syntax is the source form)
#
# Case-insensitive, whitespace-tolerant so a column like
# ``id  INTEGER   PRIMARY KEY   AUTOINCREMENT`` is matched regardless of spacing.
# Everything else in our migrations (VARCHAR/BIGINT/FLOAT/REAL/TEXT/INTEGER,
# CREATE TABLE/INDEX IF NOT EXISTS) is valid on both engines, so no other
# rewrite is needed. (Audited 0001–0005: AUTOINCREMENT is the only SQLite-ism.)

import re

_AUTOINCREMENT_RE = re.compile(
    r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.IGNORECASE
)

# PostgreSQL will NOT coerce the integer literal 0/1 to a boolean in a column
# DEFAULT (SQLite, the test DB, silently accepts it) — so `BOOLEAN DEFAULT 0`
# passes CI and then aborts the whole migration transaction on a Postgres box,
# permanently wedging the chain. Rewrite the literal to FALSE/TRUE for Postgres.
_BOOL_DEFAULT_RE = re.compile(
    r"(\bBOOLEAN\s+DEFAULT\s+)([01])\b", re.IGNORECASE
)

# Matches `ALTER TABLE <t> ADD COLUMN <c> ...` so we can skip an add-column that
# is already present. The ORM's Database.ensure_added_columns() idempotently adds
# late columns too, so a migration file (e.g. 0006/0007) that ADDs the same
# column would otherwise raise "duplicate column" on EVERY boot — the file never
# gets recorded in schema_migrations and permanently wedges all later migrations.
_ADD_COLUMN_RE = re.compile(
    r"\bALTER\s+TABLE\s+([\"'`]?)(?P<table>\w+)\1\s+ADD\s+COLUMN\s+"
    r"([\"'`]?)(?P<col>\w+)\3",
    re.IGNORECASE,
)


def _translate_for_dialect(stmt, dialect_name):
    """Rewrite SQLite-authored DDL tokens for the target SQLAlchemy dialect.

    On PostgreSQL: ``INTEGER PRIMARY KEY AUTOINCREMENT`` -> ``BIGSERIAL PRIMARY
    KEY``, and ``BOOLEAN DEFAULT 0/1`` -> ``BOOLEAN DEFAULT FALSE/TRUE`` (Postgres
    rejects an integer default on a boolean column). On sqlite (or any other
    dialect) the statement is returned unchanged.
    """
    if dialect_name == "postgresql":
        stmt = _AUTOINCREMENT_RE.sub("BIGSERIAL PRIMARY KEY", stmt)
        stmt = _BOOL_DEFAULT_RE.sub(
            lambda m: m.group(1) + ("TRUE" if m.group(2) == "1" else "FALSE"),
            stmt,
        )
    return stmt


def apply_migrations(engine):
    """Apply every pending SQL migration against the given SQLAlchemy engine.

    Idempotent: tracks applied filenames in ``schema_migrations`` and skips
    those already recorded. Returns the list of filenames applied this call.
    """
    from sqlalchemy import text, inspect

    # Branch DDL generation on the live engine's dialect so SQLite-authored
    # migrations apply cleanly on PostgreSQL (production) too — see
    # _translate_for_dialect.
    dialect_name = engine.dialect.name

    def _column_present(conn, table, col):
        """True if `table.col` already exists (so an ADD COLUMN would collide)."""
        try:
            cols = {c["name"].lower() for c in inspect(conn).get_columns(table)}
        except Exception:
            # Table may not exist yet in this file's context — let the real
            # statement run and surface any genuine error.
            return False
        return col.lower() in cols

    applied = []
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  filename VARCHAR(255) PRIMARY KEY,"
                "  applied_at VARCHAR(64)"
                ")"
            )
        )
        rows = conn.execute(text("SELECT filename FROM schema_migrations"))
        done = {r[0] for r in rows}

    for filename in _list_migration_files():
        if filename in done:
            continue
        path = os.path.join(MIGRATIONS_DIR, filename)
        with open(path, "r", encoding="utf-8") as fh:
            sql_text = fh.read()
        statements = _split_statements(sql_text)
        # One transaction per file: all-or-nothing.
        with engine.begin() as conn:
            for stmt in statements:
                # Skip an ADD COLUMN whose column already exists (e.g. the ORM's
                # ensure_added_columns() beat this migration to it). Without this
                # the duplicate-column error rolls back the file every boot and
                # wedges all later migrations. Only genuinely-present columns are
                # skipped; any other DDL — and any real error — still runs/raises.
                m = _ADD_COLUMN_RE.search(stmt)
                if m and _column_present(conn, m.group("table"), m.group("col")):
                    logger.info(
                        "Migration %s: column %s.%s already present, skipping ADD COLUMN",
                        filename, m.group("table"), m.group("col"),
                    )
                    continue
                conn.execute(text(_translate_for_dialect(stmt, dialect_name)))
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (filename, applied_at) "
                    "VALUES (:f, :t)"
                ),
                {"f": filename, "t": _now_iso()},
            )
        applied.append(filename)
        logger.info("Applied migration %s", filename)

    return applied


def _now_iso():
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()

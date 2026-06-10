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


def apply_migrations(engine):
    """Apply every pending SQL migration against the given SQLAlchemy engine.

    Idempotent: tracks applied filenames in ``schema_migrations`` and skips
    those already recorded. Returns the list of filenames applied this call.
    """
    from sqlalchemy import text

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
                conn.execute(text(stmt))
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

    return datetime.datetime.now(datetime.UTC).isoformat()

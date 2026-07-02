"""
Shared raw-SQL schema constants.

The loop/records/stats tables (db/migrations/*.sql) are deliberately kept out of
the ORM: everything that touches them (CallRecordParser, LoopMatcher, retention,
pool optimizer) does best-effort raw SQL that also works with db=None. The cost
of that style is column lists repeated across modules, which drift silently —
these constants are the single source of truth for the shared ones.

Keep the tuples in the physical column order of the CREATE TABLE in the
migration; readers zip() row tuples against them.
"""

# call_records (0003_call_records.sql). matched_record_id is intentionally NOT
# in the writer field list — only the LoopMatcher stamps it, after ingest.
CALL_RECORD_FIELDS = (
    "campaign_id", "direction", "call_uuid", "a_number", "b_number",
    "source_ip", "t_start_ms", "t_answer_ms", "t_end_ms",
    "duration_ms", "final_code", "created_at",
)

# The read shape (id first) used when selecting whole records.
CALL_RECORD_COLUMNS = ("id",) + CALL_RECORD_FIELDS

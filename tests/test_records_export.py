"""
CDR export: GET /api/loops/{id}/records.csv (streamed, keyset-paginated).
"""

import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gencall.api import loops as loops_mod
from gencall.api.loops import _parse_export_bound, _stream_records_csv
from gencall.api.routes import require_api_key
from gencall.core.call_records import CallRecordParser
from gencall.db.migrations import apply_migrations
from gencall.db.models import Database


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'export.db'}")
    database.create_tables()
    apply_migrations(database.engine)
    return database


def _seed(db, campaign_id, n, direction="out", day="2026-07-01"):
    """Insert n finished records stamped on the given (UTC) day."""
    from sqlalchemy import text

    from gencall.db.schema import CALL_RECORD_FIELDS

    sql = ("INSERT INTO call_records (" + ", ".join(CALL_RECORD_FIELDS) + ") "
           "VALUES (" + ", ".join(f":{f}" for f in CALL_RECORD_FIELDS) + ")")
    with db.engine.begin() as conn:
        for i in range(n):
            conn.execute(text(sql), {
                "campaign_id": campaign_id, "direction": direction,
                "call_uuid": f"{campaign_id}-{direction}-{day}-{i}",
                "a_number": f"1000{i:04d}", "b_number": f"2000{i:04d}",
                "source_ip": None, "t_start_ms": 1000 + i,
                "t_answer_ms": 1100 + i, "t_end_ms": 61100 + i,
                "duration_ms": 60000, "final_code": 200,
                "created_at": f"{day}T10:{i % 60:02d}:00+00:00",
            })


class FakeEngine:
    def __init__(self, db):
        self.db = db

    def get_campaign(self, cid):
        if cid != "camp-1":
            raise KeyError(cid)
        return {"id": cid}


@pytest.fixture
def client(db):
    loops_mod.loop_engine = FakeEngine(db)
    app = FastAPI()
    app.include_router(loops_mod.router)
    app.dependency_overrides[require_api_key] = lambda: None
    return TestClient(app)


def _rows(body: str):
    return list(csv.reader(io.StringIO(body)))


def test_export_streams_all_rows_with_header(client, db):
    _seed(db, "camp-1", 25)
    _seed(db, "other-camp", 5)  # must not leak into camp-1's export
    r = client.get("/api/loops/camp-1/records.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="records_camp-1.csv"' in r.headers["content-disposition"]
    rows = _rows(r.text)
    header, data = rows[0], rows[1:]
    assert header[0] == "id" and "call_uuid" in header and \
        "matched_record_id" in header
    assert len(data) == 25
    camp_col = header.index("campaign_id")
    assert all(row[camp_col] == "camp-1" for row in data)


def test_export_keyset_batches_cover_everything(db):
    """More rows than one batch -> the generator pages by id with no gaps."""
    import gencall.api.loops as mod
    _seed(db, "camp-1", 12)
    orig = mod._EXPORT_BATCH
    mod._EXPORT_BATCH = 5
    try:
        body = "".join(_stream_records_csv(db, "camp-1"))
    finally:
        mod._EXPORT_BATCH = orig
    data = _rows(body)[1:]
    assert len(data) == 12
    ids = [int(r[0]) for r in data]
    assert ids == sorted(ids) and len(set(ids)) == 12


def test_export_filters_direction_and_window(client, db):
    _seed(db, "camp-1", 3, direction="out", day="2026-07-01")
    _seed(db, "camp-1", 4, direction="in", day="2026-07-01")
    _seed(db, "camp-1", 2, direction="out", day="2026-07-03")

    r = client.get("/api/loops/camp-1/records.csv?direction=in")
    assert len(_rows(r.text)) - 1 == 4

    # until is a plain date -> inclusive of that whole day.
    r = client.get("/api/loops/camp-1/records.csv?since=2026-07-01&until=2026-07-01")
    assert len(_rows(r.text)) - 1 == 7

    r = client.get("/api/loops/camp-1/records.csv?since=2026-07-02")
    assert len(_rows(r.text)) - 1 == 2


def test_export_validation_and_404(client, db):
    assert client.get("/api/loops/camp-1/records.csv?direction=x").status_code == 422
    assert client.get("/api/loops/camp-1/records.csv?since=notadate").status_code == 422
    assert client.get("/api/loops/nope/records.csv").status_code == 404


def test_parse_export_bound_forms():
    assert _parse_export_bound("2026-07-01", is_until=False).startswith("2026-07-01T00:00:00")
    # Date-only until advances a day (query uses '<').
    assert _parse_export_bound("2026-07-01", is_until=True).startswith("2026-07-02T00:00:00")
    # Naive datetimes are treated as UTC; aware ones pass through.
    assert _parse_export_bound("2026-07-01T12:30:00", is_until=False) == \
        "2026-07-01T12:30:00+00:00"


def test_export_rows_roundtrip_from_parser(db):
    """Records written by the real parser upsert path come back out via CSV."""
    parser = CallRecordParser(db=db)
    parser._persist_many([{
        "campaign_id": "camp-1", "direction": "out", "call_uuid": "u-rt",
        "a_number": "100", "b_number": "200", "source_ip": None,
        "t_start_ms": 1, "t_answer_ms": 2, "t_end_ms": 3,
        "duration_ms": 1, "final_code": 200,
    }])
    body = "".join(_stream_records_csv(db, "camp-1"))
    rows = _rows(body)
    assert len(rows) == 2
    assert "u-rt" in rows[1]

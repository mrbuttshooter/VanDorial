"""Tests for the editable sale-zone overlay (DB model, merge, API)."""
import os

import pytest

from gencall.db.models import Database, SaleZone
from gencall.scripts import gen_loop_csv as _g

# The committed sample deck (no proprietary countries like Algeria), so the API
# tests are deck-independent regardless of whether the full sale_codes.csv is
# present on the box.
SAMPLE = os.path.join(os.path.dirname(_g.__file__), "data", "sale_codes.sample.csv")


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'zones.db'}")
    database.create_tables()
    return database


def test_salezone_table_created_and_roundtrips(db):
    session = db.get_session()
    try:
        session.add(SaleZone(country="Algeria", zone="Algeria-Mobile (Djezzy)", code="21377"))
        session.commit()
        row = session.query(SaleZone).one()
        assert row.to_dict()["country"] == "Algeria"
        assert row.to_dict()["zone"] == "Algeria-Mobile (Djezzy)"
        assert row.to_dict()["code"] == "21377"
        assert row.to_dict()["id"] > 0
    finally:
        session.close()


def test_salezone_zone_code_unique(db):
    from sqlalchemy.exc import IntegrityError
    session = db.get_session()
    try:
        session.add(SaleZone(country="Algeria", zone="Algeria-Mobile (Djezzy)", code="21377"))
        session.commit()
        session.add(SaleZone(country="Algeria", zone="Algeria-Mobile (Djezzy)", code="21377"))
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.close()


def test_db_catalog_groups_rows(db):
    from gencall.core import sale_zones
    session = db.get_session()
    try:
        session.add_all([
            SaleZone(country="Algeria", zone="Algeria-Mobile (Djezzy)", code="21377"),
            SaleZone(country="Algeria", zone="Algeria-Mobile (Djezzy)", code="213778"),
            SaleZone(country="Faketopia", zone="Faketopia-Mobile", code="999"),
        ])
        session.commit()
    finally:
        session.close()

    zones, countries = sale_zones.db_catalog(db)
    assert zones["Algeria-Mobile (Djezzy)"] == ["21377", "213778"]  # shortest-first
    assert zones["Faketopia-Mobile"] == ["999"]
    assert countries["Algeria-Mobile (Djezzy)"] == "Algeria"
    assert countries["Faketopia-Mobile"] == "Faketopia"


def test_make_provider_returns_zone_codes_only(db):
    from gencall.core import sale_zones
    session = db.get_session()
    try:
        session.add(SaleZone(country="Faketopia", zone="Faketopia-Mobile", code="999"))
        session.commit()
    finally:
        session.close()
    provider = sale_zones.make_provider(db)
    assert provider() == {"Faketopia-Mobile": ["999"]}


def _client(db, monkeypatch):
    """A TestClient over a minimal app with the loops router + this test DB,
    auth disabled."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from gencall.api import loops as loops_mod
    from gencall.api.routes import require_api_key

    # Force the committed sample deck so the catalog is deck-independent (the full
    # sale_codes.csv on this box already lists Algeria, which would mask the
    # overlay add/delete assertions). Reset the module-level deck cache too.
    monkeypatch.setenv("GENCALL_SALE_CODES", SAMPLE)
    monkeypatch.setattr(loops_mod, "_ZONES_CACHE", None, raising=False)

    class _Engine:
        def __init__(self, db): self.db = db
    loops_mod.loop_engine = _Engine(db)

    app = FastAPI()
    app.include_router(loops_mod.router)
    # The route decorators captured `require_api_key` by reference at import time,
    # so patching the name on the routes module is ineffective; override the
    # dependency on the app (FastAPI's supported test hook) to disable auth.
    app.dependency_overrides[require_api_key] = lambda: None
    return TestClient(app)


def test_post_sale_zone_then_appears_in_get(db, monkeypatch):
    client = _client(db, monkeypatch)
    r = client.post("/api/sale-zones",
                    json={"country": "Algeria", "zone": "Algeria-Mobile (Djezzy)", "code": "21377"})
    assert r.status_code == 200, r.text
    new_id = r.json()["sale_zone"]["id"]

    g = client.get("/api/sale-zones").json()
    assert any(c["name"] == "Algeria" and "Algeria-Mobile (Djezzy)" in c["zones"]
               for c in g["countries"])
    assert g["codes"]["Algeria-Mobile (Djezzy)"] == ["21377"]

    d = client.delete(f"/api/sale-zones/{new_id}")
    assert d.status_code == 200
    g2 = client.get("/api/sale-zones").json()
    assert all(c["name"] != "Algeria" for c in g2["countries"])


def test_post_sale_zone_rejects_bad_input(db, monkeypatch):
    client = _client(db, monkeypatch)
    assert client.post("/api/sale-zones",
                       json={"country": "X", "zone": "X-Z", "code": "abc"}).status_code == 422
    client.post("/api/sale-zones", json={"country": "X", "zone": "X-Z", "code": "111"})
    dup = client.post("/api/sale-zones", json={"country": "X", "zone": "X-Z", "code": "111"})
    assert dup.status_code == 409

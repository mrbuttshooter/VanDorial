"""Tests for the editable sale-zone overlay (DB model, merge, API)."""
import pytest

from gencall.db.models import Database, SaleZone


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

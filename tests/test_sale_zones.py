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

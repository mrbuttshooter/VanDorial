"""DB overlay for the sale-zone catalog (editable zones on top of the CSV deck).

Keeps the DB dependency out of gencall.scripts.gen_loop_csv: this module reads
SaleZone rows and shapes them into the {zone: [codes]} / {zone: country} maps the
catalog GET and the number generator consume.
"""

from gencall.db.models import Database, SaleZone


def db_catalog(db: Database) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Return (``{zone: [codes]}``, ``{zone: country}``) from the SaleZone table.

    Codes are de-duped per zone and kept shortest-first to match load_zones()."""
    zones: dict[str, list[str]] = {}
    countries: dict[str, str] = {}
    session = db.get_session()
    try:
        for row in session.query(SaleZone).all():
            zones.setdefault(row.zone, [])
            if row.code not in zones[row.zone]:
                zones[row.zone].append(row.code)
            countries[row.zone] = row.country
    finally:
        session.close()
    for z in zones:
        zones[z].sort(key=lambda c: (len(c), c))
    return zones, countries


def make_provider(db: Database):
    """A zero-arg callable returning the {zone: [codes]} overlay (for
    gen_loop_csv.set_overlay_provider)."""
    def _provider() -> dict[str, list[str]]:
        return db_catalog(db)[0]
    return _provider

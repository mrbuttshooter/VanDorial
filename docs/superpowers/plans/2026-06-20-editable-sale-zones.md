# Editable Sale Zones Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let operators add new sale zones (country + zone label + dial code) from the app, on top of the immutable bundled `sale_codes.csv` deck, so number generation isn't limited to the shipped catalog.

**Architecture:** A new `SaleZone` DB table is an additive **overlay** on the read-only CSV deck. The CSV stays cached as the base; DB rows merge in **live** (no cache invalidation). `gen_loop_csv` stays DB-free: the catalog GET merges explicitly, and number generation picks up DB zones through a single registered **overlay provider** so every generate call-site (standalone + per-node, local + remote) sees new zones with no per-call-site change. New zones flow through the existing routable-allowlist + E.164-length logic unchanged ("E.164 as the others work").

**Tech Stack:** Python 3.10+, FastAPI, SQLAlchemy (SQLite/PostgreSQL), pytest; React + TypeScript (Vite) frontend.

---

## File Structure

- **Create** `gencall/core/sale_zones.py` — DB overlay helpers (`db_catalog(session)`, `make_provider(db)`). One responsibility: turn `SaleZone` rows into the `{zone: [codes]}` + `{zone: country}` shapes the catalog/generator consume. Keeps the DB dependency OUT of `gen_loop_csv`.
- **Modify** `gencall/db/models.py` — add the `SaleZone` ORM model (auto-created by `create_tables()`).
- **Modify** `gencall/scripts/gen_loop_csv.py` — pure helpers: `merge_zones()`, `country_overrides` arg on `build_country_tree()`, and an overlay-provider hook consumed by `generate_pool_file()`.
- **Modify** `gencall/api/loops.py` — merge overlay into `GET /api/sale-zones`; add `POST /api/sale-zones` + `DELETE /api/sale-zones/{id}`.
- **Modify** `gencall/main.py` — register the overlay provider once at startup.
- **Modify** `frontend/src/lib/types.ts` — `SaleZoneRow`, `SaleZoneCreate`.
- **Modify** `frontend/src/lib/api.ts` — `createSaleZone()`, `deleteSaleZone()`, `listSaleZoneRows()`.
- **Modify** `frontend/src/pages/Nodes.tsx` — a "+ Add sale zone" modal and delete affordance for user-added zones.
- **Tests:** `tests/test_sale_zones.py` (new — model, overlay helper, provider, API), extend `tests/test_gen_loop_csv.py` (merge + overrides).

---

## Task 1: `SaleZone` model

**Files:**
- Modify: `gencall/db/models.py`
- Test: `tests/test_sale_zones.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_sale_zones.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sale_zones.py -v`
Expected: FAIL — `ImportError: cannot import name 'SaleZone'`.

- [ ] **Step 3: Add the model**

In `gencall/db/models.py`, extend the existing import and add the class after `class Server` (keep it near the other catalog tables). Update the top import line:

```python
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, Enum,
    UniqueConstraint, create_engine,
)
```

Add the model:

```python
class SaleZone(Base):
    """A user-added sale zone: an additive overlay on the bundled sale_codes.csv.

    The CSV deck is the immutable base catalog; rows here ADD new (zone, code)
    pairs (or extra codes for an existing zone). ``country`` is stored explicitly
    so grouping is robust regardless of the zone label. One row per (zone, code);
    a zone with several codes is several rows. Delete affects only these rows.
    """
    __tablename__ = "sale_zones"
    __table_args__ = (UniqueConstraint("zone", "code", name="uq_sale_zones_zone_code"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    country = Column(String(255), nullable=False)
    zone = Column(String(255), nullable=False)
    code = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "country": self.country,
            "zone": self.zone,
            "code": self.code,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sale_zones.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add gencall/db/models.py tests/test_sale_zones.py
git commit -m "feat(zones): add SaleZone overlay table"
```

---

## Task 2: `merge_zones()` + country overrides in `gen_loop_csv` (pure)

**Files:**
- Modify: `gencall/scripts/gen_loop_csv.py`
- Test: `tests/test_gen_loop_csv.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gen_loop_csv.py`:

```python
def test_merge_zones_adds_new_zone_and_extra_codes(zones):
    merged = g.merge_zones(zones, {
        "Algeria-Mobile (Djezzy)": ["21377"],   # brand-new zone
        "Nigeria-Lagos": ["2342"],              # extra code on an existing zone
    })
    assert merged["Algeria-Mobile (Djezzy)"] == ["21377"]
    assert merged["Nigeria-Lagos"] == ["2341", "2342"]  # shortest-first, de-duped
    # original is not mutated
    assert zones["Nigeria-Lagos"] == ["2341"]


def test_build_country_tree_uses_overrides_for_new_zones(zones):
    merged = g.merge_zones(zones, {"Algeria-Mobile (Djezzy)": ["21377"]})
    tree = g.build_country_tree(merged, country_overrides={"Algeria-Mobile (Djezzy)": "Algeria"})
    assert "Algeria" in tree
    assert tree["Algeria"] == ["Algeria-Mobile (Djezzy)"]
    # unrelated zones still grouped by derived country
    assert "Nigeria-Lagos" in tree["Nigeria"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_gen_loop_csv.py -k "merge_zones or overrides" -v`
Expected: FAIL — `AttributeError: module 'gencall.scripts.gen_loop_csv' has no attribute 'merge_zones'`.

- [ ] **Step 3: Implement**

In `gencall/scripts/gen_loop_csv.py`, add `merge_zones()` just below `load_zones()`:

```python
def merge_zones(base: Dict[str, List[str]],
                extra: Optional[Dict[str, List[str]]]) -> "OrderedDict[str, List[str]]":
    """Return a NEW ``{zone: [codes]}`` map = ``base`` plus ``extra`` (the DB
    overlay). Extra codes are appended to an existing zone (de-duped) or create a
    new zone. Codes stay shortest-first. ``base`` is never mutated."""
    merged: "OrderedDict[str, List[str]]" = OrderedDict(
        (z, list(codes)) for z, codes in base.items()
    )
    for zone, codes in (extra or {}).items():
        cur = merged.setdefault(zone, [])
        for c in codes:
            if c not in cur:
                cur.append(c)
        cur.sort(key=lambda c: (len(c), c))
    return merged
```

Change `build_country_tree` to accept overrides:

```python
def build_country_tree(zones: Dict[str, List[str]],
                       country_overrides: Optional[Dict[str, str]] = None
                       ) -> "OrderedDict[str, List[str]]":
    """Group zone names by country: ``country -> [zone, ...]`` (sorted).

    Country is ``country_overrides[zone]`` when present (DB overlay rows carry an
    explicit country), else derived from the zone name."""
    overrides = country_overrides or {}
    tree: "OrderedDict[str, List[str]]" = OrderedDict()
    for zone in zones:
        country = overrides.get(zone) or derive_country(zone)
        tree.setdefault(country, []).append(zone)
    out: "OrderedDict[str, List[str]]" = OrderedDict()
    for country in sorted(tree):
        out[country] = sorted(tree[country])
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_gen_loop_csv.py -v`
Expected: PASS (new tests + all existing zone/tree tests still green).

- [ ] **Step 5: Commit**

```bash
git add gencall/scripts/gen_loop_csv.py tests/test_gen_loop_csv.py
git commit -m "feat(zones): merge_zones helper + country_overrides in build_country_tree"
```

---

## Task 3: Overlay provider hook + `generate_pool_file(extra_zones=...)`

**Files:**
- Modify: `gencall/scripts/gen_loop_csv.py`
- Test: `tests/test_gen_loop_csv.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gen_loop_csv.py`:

```python
def test_generate_pool_file_honors_extra_zones(tmp_path, monkeypatch):
    # Force the sample deck so the test is deck-independent.
    monkeypatch.setenv("GENCALL_SALE_CODES", SAMPLE)
    path, n, preview = g.generate_pool_file(
        origin_zone="Nigeria-Lagos", dest_zone="Faketopia-Mobile",
        count=5, length=11, seed=1,
        extra_zones={"Faketopia-Mobile": ["999"]},
    )
    assert n == 5
    for row in preview:
        a, b = row.split(";")
        assert b.startswith("999")


def test_overlay_provider_is_used_when_no_explicit_extra(tmp_path, monkeypatch):
    monkeypatch.setenv("GENCALL_SALE_CODES", SAMPLE)
    g.set_overlay_provider(lambda: {"Faketopia-Mobile": ["999"]})
    try:
        path, n, preview = g.generate_pool_file(
            origin_zone="Nigeria-Lagos", dest_zone="Faketopia-Mobile",
            count=3, length=11, seed=2,
        )
        assert all(row.split(";")[1].startswith("999") for row in preview)
    finally:
        g.set_overlay_provider(None)  # reset global
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_gen_loop_csv.py -k "extra_zones or overlay_provider" -v`
Expected: FAIL — `TypeError: generate_pool_file() got an unexpected keyword argument 'extra_zones'` / `no attribute 'set_overlay_provider'`.

- [ ] **Step 3: Implement**

In `gencall/scripts/gen_loop_csv.py`, add the provider hook near the top of the "Pair generation" section:

```python
# Optional overlay provider: a zero-arg callable returning {zone: [codes]} of
# user-added sale zones (the DB overlay). Registered once at app startup
# (gencall/main.py) so every generate_pool_file() call sees new zones without
# threading a DB session through. Stays None in the pure CLI / tests.
_overlay_provider = None


def set_overlay_provider(fn) -> None:
    """Register (or clear, with None) the sale-zone overlay provider."""
    global _overlay_provider
    _overlay_provider = fn


def _overlay_zones() -> Dict[str, List[str]]:
    if _overlay_provider is None:
        return {}
    try:
        return _overlay_provider() or {}
    except Exception:
        _log.warning("sale-zone overlay provider failed; using base deck only", exc_info=True)
        return {}
```

Change `generate_pool_file` to merge the overlay (explicit `extra_zones` wins; else the provider):

```python
def generate_pool_file(origin_zone, dest_zone, count=500000, length=11,
                       seed=None, origin_code="", dest_code="", out_dir=None,
                       oad_length=None, dad_length=None, extra_zones=None):
    """... (existing docstring) ...

    ``extra_zones`` ({zone: [codes]}) is merged onto the deck before generation;
    when omitted, the registered overlay provider supplies it (DB-added zones).
    """
    import os
    import tempfile

    zones = load_zones(resolve_deck_path())
    overlay = extra_zones if extra_zones is not None else _overlay_zones()
    zones = merge_zones(zones, overlay)
    pairs = generate_pairs(
        zones,
        oad_zone=origin_zone, oad_code=origin_code or None,
        dad_zone=dest_zone, dad_code=dest_code or None,
        count=count, length=length, seed=seed,
        oad_length=oad_length, dad_length=dad_length,
    )
    out_dir = out_dir or os.path.join(tempfile.gettempdir(), "gencall_numbers")
    os.makedirs(out_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="numbers_", suffix=".csv", dir=out_dir)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        write_csv(pairs, fh)
    preview = [f"{a};{b}" for a, b in pairs[:10]]
    return path, len(pairs), preview
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_gen_loop_csv.py -v`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add gencall/scripts/gen_loop_csv.py tests/test_gen_loop_csv.py
git commit -m "feat(zones): overlay provider + extra_zones merge in generate_pool_file"
```

---

## Task 4: DB overlay helper (`gencall/core/sale_zones.py`)

**Files:**
- Create: `gencall/core/sale_zones.py`
- Test: `tests/test_sale_zones.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sale_zones.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_sale_zones.py -k "db_catalog or make_provider" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gencall.core.sale_zones'`.

- [ ] **Step 3: Implement**

Create `gencall/core/sale_zones.py`:

```python
"""DB overlay for the sale-zone catalog (editable zones on top of the CSV deck).

Keeps the DB dependency out of gencall.scripts.gen_loop_csv: this module reads
SaleZone rows and shapes them into the {zone: [codes]} / {zone: country} maps the
catalog GET and the number generator consume.
"""
from typing import Dict, List, Tuple

from gencall.db.models import Database, SaleZone


def db_catalog(db: Database) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """Return (``{zone: [codes]}``, ``{zone: country}``) from the SaleZone table.

    Codes are de-duped per zone and kept shortest-first to match load_zones()."""
    zones: Dict[str, List[str]] = {}
    countries: Dict[str, str] = {}
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
    def _provider() -> Dict[str, List[str]]:
        return db_catalog(db)[0]
    return _provider
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_sale_zones.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gencall/core/sale_zones.py tests/test_sale_zones.py
git commit -m "feat(zones): DB overlay helper (db_catalog + provider factory)"
```

---

## Task 5: Register the overlay provider at startup

**Files:**
- Modify: `gencall/main.py`

- [ ] **Step 1: Locate the wiring point**

Run: `python - <<'PY'` to find where the DB and LoopEngine are wired in `create_app`:
```python
import re, io
src = open("gencall/main.py", encoding="utf-8").read()
for m in re.finditer(r".*(Database\(|create_tables|loop_engine|LoopEngine|db =).*", src):
    print(m.group(0).strip())
PY
```
Expected: prints the lines where `db` (a `Database`) is constructed and tables created.

- [ ] **Step 2: Register the provider**

In `gencall/main.py`, immediately AFTER the `Database` is constructed and `create_tables()` has run (so `sale_zones` exists), add:

```python
# Make DB-added sale zones visible to every number-generation call (standalone
# and per-node) without threading a session through gen_loop_csv.
from gencall.core import sale_zones as _sale_zones
from gencall.scripts import gen_loop_csv as _gen_loop_csv
_gen_loop_csv.set_overlay_provider(_sale_zones.make_provider(db))
```

(If `create_app` runs without a DB — e.g. a degraded mode — guard with `if db:` matching the surrounding style.)

- [ ] **Step 3: Smoke test the app imports + wires**

Run: `python -c "from gencall.main import create_app; app, cfg = create_app(); print('ok')"`
Expected: prints `ok` with no exception.

- [ ] **Step 4: Commit**

```bash
git add gencall/main.py
git commit -m "feat(zones): register sale-zone overlay provider at startup"
```

---

## Task 6: API — GET reflects overlay, POST + DELETE

**Files:**
- Modify: `gencall/api/loops.py`
- Test: `tests/test_sale_zones.py`

- [ ] **Step 1: Write the failing test (FastAPI TestClient)**

Append to `tests/test_sale_zones.py`:

```python
def _client(db, monkeypatch):
    """A TestClient over a minimal app with the loops router + this test DB,
    auth disabled."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from gencall.api import loops as loops_mod
    from gencall.api import routes as routes_mod

    monkeypatch.setattr(routes_mod, "require_api_key", lambda: None, raising=False)

    class _Engine:
        def __init__(self, db): self.db = db
    loops_mod.loop_engine = _Engine(db)

    app = FastAPI()
    app.include_router(loops_mod.router)
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_sale_zones.py -k "appears_in_get or rejects_bad_input" -v`
Expected: FAIL — `405`/`404` (POST/DELETE routes not defined).

- [ ] **Step 3: Implement the merged GET + POST + DELETE**

In `gencall/api/loops.py`, add an import near the top (with the other `gencall.*` imports):

```python
from gencall.core import sale_zones as sale_zones_db
```

Replace the body of `sale_zones()` so it merges the overlay, and add the helper + the two new endpoints right after it:

```python
def _merged_catalog():
    """(zones, country_overrides, country_tree) = CSV deck + DB overlay."""
    base = _zones()                                  # cached CSV {zone: [codes]}
    db = getattr(_engine(), "db", None)
    if db is None:
        merged = gen_loop_csv.merge_zones(base, {})
        return merged, {}, gen_loop_csv.build_country_tree(merged)
    db_zones, overrides = sale_zones_db.db_catalog(db)
    merged = gen_loop_csv.merge_zones(base, db_zones)
    tree = gen_loop_csv.build_country_tree(merged, country_overrides=overrides)
    return merged, overrides, tree


@router.get("/api/sale-zones", dependencies=[Depends(require_api_key)])
def sale_zones():
    """Country -> [sale zones] tree + zone -> [codes] map (CSV deck + DB overlay)."""
    try:
        merged, _overrides, tree = _merged_catalog()
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    return {
        "countries": [{"name": c, "zones": zs} for c, zs in tree.items()],
        "codes": {z: list(codes) for z, codes in merged.items()},
    }


class SaleZoneCreate(BaseModel):
    country: str
    zone: str
    code: str

    @field_validator("country", "zone")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("code")
    @classmethod
    def _digits(cls, v: str) -> str:
        v = (v or "").strip()
        if not v.isdigit():
            raise ValueError("code must be digits")
        return v


@router.post("/api/sale-zones", dependencies=[Depends(require_api_key)])
def create_sale_zone(req: SaleZoneCreate):
    """Add a user sale zone (overlay on the CSV deck). 409 on duplicate (zone, code)."""
    from sqlalchemy.exc import IntegrityError
    from gencall.db.models import SaleZone

    db = getattr(_engine(), "db", None)
    if db is None:
        raise HTTPException(503, "Database not configured on this worker")
    session = db.get_session()
    try:
        row = SaleZone(country=req.country, zone=req.zone, code=req.code)
        session.add(row)
        session.commit()
        return {"status": "created", "sale_zone": row.to_dict()}
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, f"zone/code already exists: {req.zone} / {req.code}")
    finally:
        session.close()


@router.delete("/api/sale-zones/{sale_zone_id}", dependencies=[Depends(require_api_key)])
def delete_sale_zone(sale_zone_id: int):
    """Delete a user-added sale zone by id (bundled CSV zones are not deletable)."""
    from gencall.db.models import SaleZone

    db = getattr(_engine(), "db", None)
    if db is None:
        raise HTTPException(503, "Database not configured on this worker")
    session = db.get_session()
    try:
        row = session.query(SaleZone).filter_by(id=sale_zone_id).first()
        if not row:
            raise HTTPException(404, f"sale zone {sale_zone_id} not found")
        session.delete(row)
        session.commit()
        return {"status": "deleted", "id": sale_zone_id}
    finally:
        session.close()
```

> Note: `field_validator` is already imported in `loops.py` (`from pydantic import BaseModel, Field, field_validator`). Generation already picks up the overlay via the provider (Task 5), so `generate_numbers`/node-pool generation need no change.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_sale_zones.py -v`
Expected: PASS. Then the full suite: `python -m pytest tests/test_gen_loop_csv.py tests/test_sale_zones.py -v` → all green.

- [ ] **Step 5: Commit**

```bash
git add gencall/api/loops.py tests/test_sale_zones.py
git commit -m "feat(zones): GET merges overlay; POST/DELETE /api/sale-zones"
```

---

## Task 7: Frontend types + API client

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/api.ts`

- [ ] **Step 1: Add the types**

In `frontend/src/lib/types.ts`, near `SaleZonesResponse` (~line 383), add:

```typescript
export interface SaleZoneRow {
  id: number;
  country: string;
  zone: string;
  code: string;
  created_at: string | null;
}

export interface SaleZoneCreate {
  country: string;
  zone: string;
  code: string;
}
```

- [ ] **Step 2: Add the client methods**

In `frontend/src/lib/api.ts`, add `SaleZoneCreate`/`SaleZoneRow` to the type import block (lines 6–29), then add below the existing `saleZones:` line (~line 249):

```typescript
  createSaleZone: (req: SaleZoneCreate) =>
    request<{ status: string; sale_zone: SaleZoneRow }>("/api/sale-zones", {
      method: "POST",
      body: req,
    }),
  deleteSaleZone: (id: number) =>
    request<{ status: string; id: number }>(`/api/sale-zones/${id}`, {
      method: "DELETE",
    }),
```

- [ ] **Step 3: Type-check**

Run: `cd frontend && npm run build`
Expected: build succeeds (no TS errors). If the repo uses `tsc --noEmit` via a `typecheck` script, run that instead: `cd frontend && npm run typecheck`.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/lib/api.ts
git commit -m "feat(zones): frontend types + createSaleZone/deleteSaleZone client"
```

---

## Task 8: Frontend — "+ Add sale zone" modal on the Nodes page

**Files:**
- Modify: `frontend/src/pages/Nodes.tsx`

- [ ] **Step 1: Add modal state + handler**

In `Nodes.tsx`, extend the imports to include `SaleZoneCreate` type usage via `api` (no new import needed) and add state near the other `useState` hooks (after line 71):

```typescript
  const [showZone, setShowZone] = useState(false);
  const [zoneForm, setZoneForm] = useState({ country: "", zone: "", code: "" });
  const [zoneBusy, setZoneBusy] = useState(false);

  const saveZone = async () => {
    const country = zoneForm.country.trim();
    const zone = zoneForm.zone.trim();
    const code = zoneForm.code.trim();
    if (!country || !zone || !/^\d+$/.test(code)) {
      toast.error("Country, zone, and a digits-only code are required.");
      return;
    }
    setZoneBusy(true);
    try {
      await api.createSaleZone({ country, zone, code });
      toast.ok(`Sale zone added · ${zone} (${code})`);
      setShowZone(false);
      setZoneForm({ country: "", zone: "", code: "" });
      zoneTree.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setZoneBusy(false);
    }
  };
```

- [ ] **Step 2: Add the toolbar button**

In the toolbar (after the "Add Node" button, ~line 231), add:

```tsx
        <Button variant="ghost" onClick={() => setShowZone(true)}>
          <IconPlus /> Add sale zone
        </Button>
```

- [ ] **Step 3: Add the modal**

Just before the closing `</>` of the component (after the node `</Modal>`, ~line 494), add:

```tsx
      <Modal
        open={showZone}
        title={<><IconPlus /> Add sale zone</>}
        onClose={() => setShowZone(false)}
        footer={
          <ModalActions
            onCancel={() => setShowZone(false)}
            onConfirm={saveZone}
            confirmLabel={zoneBusy ? "Adding…" : "Add zone"}
            disabled={zoneBusy}
          />
        }
      >
        <p style={{ color: "var(--text-muted)", fontSize: "var(--fs-sm)", marginTop: 0 }}>
          Adds a zone on top of the bundled catalog. The number length is resolved
          the same way as every other zone (by dial code).
        </p>
        <FieldRow>
          <Field label="Country" hint="Groups the zone in the picker.">
            <input
              value={zoneForm.country}
              onChange={(e) => setZoneForm((f) => ({ ...f, country: e.target.value }))}
              placeholder="Algeria"
            />
          </Field>
          <Field label="Sale zone label" hint="Shown under the country.">
            <input
              value={zoneForm.zone}
              onChange={(e) => setZoneForm((f) => ({ ...f, zone: e.target.value }))}
              placeholder="Algeria-Mobile (Djezzy)"
            />
          </Field>
          <Field label="Dial code" hint="Digits only.">
            <input
              value={zoneForm.code}
              onChange={(e) => setZoneForm((f) => ({ ...f, code: e.target.value }))}
              placeholder="21377"
            />
          </Field>
        </FieldRow>
      </Modal>
```

- [ ] **Step 4: Build + manual verify**

Run: `cd frontend && npm run build`
Expected: build succeeds.

Manual check (with a backend running): open the Nodes page → click **Add sale zone** → enter `Algeria` / `Algeria-Mobile (Djezzy)` / `21377` → Add. Then open **Add Node**: the **Origin/Drop country** dropdown now lists **Algeria**, whose zone and code `21377` are selectable. Creating a node with it generates numbers starting `21377`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/Nodes.tsx
git commit -m "feat(zones): + Add sale zone modal on the Nodes page"
```

---

## Self-Review

**Spec coverage (spec §2):**
- "New `SaleZone` DB table as overlay" → Task 1. ✓
- "merge CSV base + DB rows" → Tasks 2, 6 (`merge_zones`, `_merged_catalog`). ✓
- "POST/DELETE /api/sale-zones, cache" → Task 6. (Refinement vs spec: the CSV stays cached; DB rows are read live, so there is **no cache to invalidate** — simpler and always-correct. Noted intentionally.) ✓
- "new zone flows into generate_pairs via merged map; existing E.164 logic" → Tasks 3 + 5 (provider) + the E.164 path is untouched. ✓
- "+ Add sale zone UI; refresh cascade" → Task 8. ✓
- "DELETE only user rows" → Task 6 (deletes by `SaleZone.id`; CSV zones have no id). ✓
- Spec §2.6 assumption (one Postgres): for a **remote** node, `_generate_node_pool` proxies to the worker's `/api/loops/numbers`, which applies **that worker's** overlay provider — correct only if the worker shares the controller DB (or has the zone). Carry this open item to execution; it does not change any code here.

**Placeholder scan:** none — every code step is concrete.

**Type consistency:** `SaleZone` columns (country/zone/code) match `to_dict()`, `db_catalog()`, `SaleZoneCreate`, `SaleZoneRow`, and the frontend form keys. `set_overlay_provider`/`_overlay_zones`/`make_provider` names align across Tasks 3–5. `_merged_catalog()` returns `(merged, overrides, tree)` and the GET uses `merged`+`tree`.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-20-editable-sale-zones.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**

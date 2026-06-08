"""
VanDorial Fleet Controller — data model (design §3).

The controller keeps its OWN database, separate from any worker DB. It stores the
node inventory, groups, and fleet-run history. API keys for authenticating the
browser→controller calls are stored using the SAME `api_keys` table + auth
utilities as the worker (we import the worker's APIKey ORM row and APIKeyManager
rather than reinventing) — see design §6.

Fields mirror the spec:
  - Node:     id, name, address, group_id?, api_key, enabled, created_at,
              last_seen, last_health (JSON), online (derived).
  - Group:    id, name, description, created_at.
  - FleetRun: id, name, group_id?, node_ids (JSON), scenario,
              destination (JSON), rate_mode, rate_value, status,
              started_at, completed_at, results (JSON).
"""

import datetime
import json
from typing import Any, Optional

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

# Reuse the worker's APIKey table + auth utilities for the controller admin key
# rather than defining a parallel key store. Importing the ORM row binds it to
# this controller Base via shared metadata below.
from gencall.db.models import APIKey  # noqa: F401  (re-exported for table create)

Base = declarative_base()

# Bind the worker's APIKey table into THIS controller metadata so
# ControllerDatabase.create_tables() also provisions the `api_keys` table the
# APIKeyManager(db=...) expects. The worker's Base.metadata holds the Table
# object; copy it into our metadata under the same name if not already present.
try:  # pragma: no cover - defensive; both metadatas are module-level constants
    from gencall.db.models import Base as _WorkerBase
    if "api_keys" not in Base.metadata.tables and "api_keys" in _WorkerBase.metadata.tables:
        _WorkerBase.metadata.tables["api_keys"].to_metadata(Base.metadata)
except Exception:  # pragma: no cover
    pass


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _as_json(value: Any, default):
    """Parse a JSON-text column into a Python object, tolerating None/garbage."""
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _iso(dt: Optional[datetime.datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# ─── Group ─────────────────────────────────────────────────────────────────────

class Group(Base):
    """A logical grouping of nodes (Node→Group is many-to-one via group_id)."""
    __tablename__ = "fleet_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "created_at": _iso(self.created_at),
        }


# ─── Node ──────────────────────────────────────────────────────────────────────

class Node(Base):
    """A worker node in the fleet inventory."""
    __tablename__ = "fleet_nodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    # Base URL, e.g. https://10.0.0.5:8080 (no trailing slash by convention).
    address = Column(String(512), nullable=False)
    group_id = Column(Integer, nullable=True)
    # Per-node worker API key, sent as X-API-Key on controller→node calls.
    api_key = Column(Text, default="")
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_seen = Column(DateTime, nullable=True)
    # JSON blob of the most recent /api/health response: version, active_tests,
    # status (+ derived "online").
    last_health = Column(Text, default="")

    def health(self) -> dict:
        return _as_json(self.last_health, {})

    @property
    def online(self) -> bool:
        """Derived from the last health probe result."""
        return bool(self.health().get("online", False))

    def to_dict(self) -> dict:
        """Raw DB shape (not the API NodeView — see routes._node_view)."""
        h = self.health()
        return {
            "id": self.id,
            "name": self.name,
            "address": self.address,
            "group_id": self.group_id,
            "enabled": bool(self.enabled),
            "online": self.online,
            "last_seen": _iso(self.last_seen),
            "version": h.get("version"),
            "active_tests": h.get("active_tests"),
            "error": h.get("error"),
            "created_at": _iso(self.created_at),
        }


# ─── FleetRun ──────────────────────────────────────────────────────────────────

class FleetRun(Base):
    """A fleet campaign: a fan-out launch of one scenario to many nodes."""
    __tablename__ = "fleet_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), default="")
    group_id = Column(Integer, nullable=True)
    node_ids = Column(Text, default="[]")          # JSON list[int]
    scenario = Column(String(255), default="")
    destination = Column(Text, default="{}")       # JSON {remote_host,remote_port,transport}
    rate_mode = Column(String(20), default="per_node")  # per_node | total
    rate_value = Column(String(64), default="0")        # stored as text-safe number
    # pending | running | partial | stopped | completed | failed
    status = Column(String(20), default="pending")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    # JSON list of per-node dicts: {node_id, ok, test_id, error}
    results = Column(Text, default="[]")

    def get_node_ids(self) -> list:
        return _as_json(self.node_ids, [])

    def get_destination(self) -> dict:
        return _as_json(self.destination, {})

    def get_results(self) -> list:
        return _as_json(self.results, [])

    def to_dict(self) -> dict:
        """FleetRunView — includes per-node results (design §4)."""
        try:
            rate_value = float(self.rate_value)
        except (TypeError, ValueError):
            rate_value = 0.0
        return {
            "id": self.id,
            "name": self.name or "",
            "group_id": self.group_id,
            "node_ids": self.get_node_ids(),
            "scenario": self.scenario,
            "destination": self.get_destination(),
            "rate": {"mode": self.rate_mode, "value": rate_value},
            "rate_mode": self.rate_mode,
            "rate_value": rate_value,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "results": self.get_results(),
        }


# ─── Database manager ──────────────────────────────────────────────────────────

class ControllerDatabase:
    """Controller database connection manager.

    Mirrors gencall.db.models.Database (engine/session/create_tables) so the rest
    of the code uses the same shape. Provisions the fleet tables AND the worker's
    `api_keys` table (bound into our Base metadata above) so the shared
    APIKeyManager(db=...) works against this same database.
    """

    def __init__(self, db_url: str):
        self.engine = create_engine(db_url, echo=False)
        self.SessionLocal = sessionmaker(bind=self.engine)

    def create_tables(self):
        Base.metadata.create_all(self.engine)

    def get_session(self):
        return self.SessionLocal()

    def drop_tables(self):
        Base.metadata.drop_all(self.engine)

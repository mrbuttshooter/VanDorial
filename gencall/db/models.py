"""
GenCall Database Models.
Uses SQLAlchemy for ORM with SQLite/PostgreSQL support.
"""

import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, Enum,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class Connector(Base):
    """A SIP connector/endpoint configuration."""
    __tablename__ = "connectors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text, default="")
    local_ip = Column(String(45), nullable=False)
    local_port = Column(Integer, default=5060)
    remote_ip = Column(String(45), nullable=False)
    remote_port = Column(Integer, default=5060)
    transport = Column(String(10), default="udp")  # udp, tcp, tls
    auth_user = Column(String(255), default="")
    auth_pass = Column(String(255), default="")
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "local_ip": self.local_ip,
            "local_port": self.local_port,
            "remote_ip": self.remote_ip,
            "remote_port": self.remote_port,
            "transport": self.transport,
            "auth_user": self.auth_user,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class NodeGroup(Base):
    """A named set of origination nodes sharing a destination route.

    Groups nodes "by customer / route": the group stores the shared loop settings
    (MADA destination + rate/duration/targets), so starting the group fans a loop
    out to EVERY member node — each on its own source IP + number pool, one loop
    per IP — and stopping the group stops them all.
    """
    __tablename__ = "node_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text, default="")
    # Shared loop settings applied to every member node on group start.
    dest_host = Column(String(255), default="")
    dest_port = Column(Integer, default=5060)
    transport = Column(String(10), default="udp")
    rate = Column(Float, default=1.0)
    max_concurrent = Column(Integer, default=10)
    duration_mode = Column(String(10), default="fixed")
    duration_s = Column(Integer, default=180)
    duration_max_s = Column(Integer, default=0)
    match_key = Column(String(20), default="exact")
    target_calls = Column(Integer, default=0)
    target_minutes = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "dest_host": self.dest_host or "",
            "dest_port": self.dest_port or 5060,
            "transport": self.transport or "udp",
            "rate": self.rate or 1.0,
            "max_concurrent": self.max_concurrent or 10,
            "duration_mode": self.duration_mode or "fixed",
            "duration_s": self.duration_s or 0,
            "duration_max_s": self.duration_max_s or 0,
            "match_key": self.match_key or "exact",
            "target_calls": self.target_calls or 0,
            "target_minutes": self.target_minutes or 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class LoopPreset(Base):
    """A saved, re-runnable loop "recipe" (design: presets + history).

    A preset stores everything about a loop EXCEPT where it originates from: the
    MADA destination, the ACD (duration), rate, concurrency, match key and
    targets. At run time the operator picks WHICH source-IP node (or which group)
    fires it — so one recipe ("Guinea-Orange @ 1.90 ACD") can be launched from any
    node or fanned out across a group without re-typing the form. Each run spawns
    a normal loop_campaign, which is what the History tab lists.
    """
    __tablename__ = "loop_presets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text, default="")
    dest_host = Column(String(255), default="")
    dest_port = Column(Integer, default=5060)
    transport = Column(String(10), default="udp")
    rate = Column(Float, default=1.0)
    max_concurrent = Column(Integer, default=10)
    duration_mode = Column(String(10), default="fixed")
    duration_s = Column(Integer, default=180)
    duration_max_s = Column(Integer, default=0)
    match_key = Column(String(20), default="exact")
    target_calls = Column(Integer, default=0)
    target_minutes = Column(Integer, default=0)
    # Stream real RTP media (PCMA pcap) on each call when True; signaling-only
    # (no media on the wire) when False. Stored 0/1.
    rtp = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "dest_host": self.dest_host or "",
            "dest_port": self.dest_port or 5060,
            "transport": self.transport or "udp",
            "rate": self.rate or 1.0,
            "max_concurrent": self.max_concurrent or 10,
            "duration_mode": self.duration_mode or "fixed",
            "duration_s": self.duration_s or 0,
            "duration_max_s": self.duration_max_s or 0,
            "match_key": self.match_key or "exact",
            "target_calls": self.target_calls or 0,
            "target_minutes": self.target_minutes or 0,
            "rtp": bool(self.rtp),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Server(Base):
    """A named origination server = a source IP a loop can run from.

    "Node = IP": each loop binds its outbound UAC to a server's ``ip`` (-i/-mi),
    and the engine enforces one running loop per IP. On a single box these are
    the box's own NIC addresses; the same record extends to a remote fleet node
    later (``api_url`` reserved for that, unused today). The user adds these in
    the console and picks one from a dropdown when starting a loop.
    """
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    ip = Column(String(45), nullable=False)
    description = Column(Text, default="")
    # Remote-worker binding: when api_url is set this node lives on ANOTHER box,
    # and its pool generation + loop start are proxied to that worker (one
    # controller, many workers). Blank api_url => local node on this box.
    api_url = Column(String(512), default="")   # http://host:port of the worker
    api_key = Column(String(255), default="")   # that worker's X-API-Key (secret)
    enabled = Column(Boolean, default=True)
    group_id = Column(Integer, default=None)  # optional NodeGroup membership
    # Per-node number pool ("each IP one loop", so the node IS the loop unit):
    # the origin/drop sale zones it dials and the generated A/B pool file.
    origin_zone = Column(String(255), default="")
    dest_zone = Column(String(255), default="")
    # Optional pinned code within each zone (e.g. dial ONLY 22462, not the whole
    # Guinea-Orange zone whose 224720/224721 breakouts the switch won't route).
    # Empty => spread across all of the zone's codes (previous behaviour).
    origin_code = Column(String(32), default="")
    dest_code = Column(String(32), default="")
    pool_count = Column(Integer, default=0)
    pool_length = Column(Integer, default=11)
    csv_path = Column(String(1024), default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "ip": self.ip,
            "description": self.description,
            "enabled": self.enabled,
            "group_id": self.group_id,
            "api_url": self.api_url or "",
            "remote": bool(self.api_url),
            "has_key": bool(self.api_key),
            "origin_zone": self.origin_zone or "",
            "dest_zone": self.dest_zone or "",
            "origin_code": self.origin_code or "",
            "dest_code": self.dest_code or "",
            "pool_count": self.pool_count or 0,
            "pool_length": self.pool_length or 11,
            "csv_path": self.csv_path or "",
            "has_pool": bool(self.csv_path),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Scenario(Base):
    """A saved SIP test scenario."""
    __tablename__ = "scenarios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text, default="")
    xml_content = Column(Text, nullable=False)
    mode = Column(String(10), default="uac")  # uac or uas
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "mode": self.mode,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TestRun(Base):
    """A record of a test execution."""
    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    connector_name = Column(String(255), default="")
    scenario_name = Column(String(255), default="")
    status = Column(String(20), default="pending")  # pending, running, completed, failed, stopped
    call_rate = Column(Float, default=1.0)
    max_calls = Column(Integer, default=0)
    call_limit = Column(Integer, default=10)
    duration = Column(Integer, default=0)
    total_calls = Column(Integer, default=0)
    successful_calls = Column(Integer, default=0)
    failed_calls = Column(Integer, default=0)
    avg_response_time_ms = Column(Float, default=0.0)
    error_message = Column(Text, default="")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "connector_name": self.connector_name,
            "scenario_name": self.scenario_name,
            "status": self.status,
            "call_rate": self.call_rate,
            "max_calls": self.max_calls,
            "call_limit": self.call_limit,
            "duration": self.duration,
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "avg_response_time_ms": self.avg_response_time_ms,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class User(Base):
    """Application user for web interface auth."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), default="operator")  # admin, operator, viewer
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class APIKey(Base):
    """A persisted API key for authenticating REST API requests.

    Only the SHA-256 hash of the raw key is stored — the raw `gc_...` token is
    shown once at creation time and never again.
    """
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_id = Column(String(64), unique=True, nullable=False)
    key_hash = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    permissions = Column(String(255), default="read,execute")  # comma-separated
    rate_limit = Column(Integer, default=60)  # requests per minute
    request_count = Column(Integer, default=0)
    enabled = Column(Boolean, default=True)
    created_at = Column(Float, default=0.0)   # epoch seconds
    last_used = Column(Float, default=0.0)     # epoch seconds

    def to_dict(self):
        return {
            "key_id": self.key_id,
            "name": self.name,
            "permissions": (self.permissions or "").split(",") if self.permissions else [],
            "rate_limit": self.rate_limit,
            "request_count": self.request_count,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_used": self.last_used,
        }


class Database:
    """Database connection manager."""

    def __init__(self, db_url: str):
        self.engine = create_engine(db_url, echo=False)
        self.SessionLocal = sessionmaker(bind=self.engine)

    def create_tables(self):
        Base.metadata.create_all(self.engine)
        self.ensure_added_columns()

    # Columns added to an ORM table AFTER it was first created. create_all() makes
    # NEW tables with all columns but never ALTERs an existing one, so a box that
    # created `servers` before the per-node number-pool columns existed would be
    # missing them (every node op would 500). Add any missing ones idempotently; a
    # duplicate-column error (fresh DB where create_all already added them) or an
    # absent table is ignored. Each ALTER runs in its own transaction so one
    # "already exists" does not abort the rest. SQLite 3.37 + PostgreSQL both
    # support single-column ``ALTER TABLE ... ADD COLUMN``.
    _ADDED_COLUMNS = {
        "servers": [
            ("origin_zone", "VARCHAR(255) DEFAULT ''"),
            ("dest_zone", "VARCHAR(255) DEFAULT ''"),
            ("origin_code", "VARCHAR(32) DEFAULT ''"),
            ("dest_code", "VARCHAR(32) DEFAULT ''"),
            ("pool_count", "INTEGER DEFAULT 0"),
            ("pool_length", "INTEGER DEFAULT 11"),
            ("csv_path", "VARCHAR(1024) DEFAULT ''"),
            ("group_id", "INTEGER"),
            ("api_url", "VARCHAR(512) DEFAULT ''"),
            ("api_key", "VARCHAR(255) DEFAULT ''"),
        ],
        "loop_presets": [
            ("rtp", "INTEGER DEFAULT 0"),
        ],
    }

    def ensure_added_columns(self):
        from sqlalchemy import text

        for table, cols in self._ADDED_COLUMNS.items():
            for col, ddl in cols:
                try:
                    with self.engine.begin() as conn:
                        conn.execute(
                            text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                        )
                except Exception:
                    pass  # already present (or table absent) — safe to ignore

    def get_session(self):
        return self.SessionLocal()

    def drop_tables(self):
        Base.metadata.drop_all(self.engine)

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

    def get_session(self):
        return self.SessionLocal()

    def drop_tables(self):
        Base.metadata.drop_all(self.engine)

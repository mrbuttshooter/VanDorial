"""
GenCall Call Detail Records (CDR) Engine.
Captures, stores, queries, and exports CDRs with real-time streaming
and telecom-grade aggregate statistics (ASR, ACD, NER, etc.).
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from gencall.core.config import Config

logger = logging.getLogger("gencall.cdr")

CDRBase = declarative_base()


# ─── Call Status ──────────────────────────────────────────────────────────────

class CallStatus(Enum):
    INITIATED = "initiated"
    RINGING = "ringing"
    ANSWERED = "answered"
    COMPLETED = "completed"
    FAILED = "failed"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    ERROR = "error"


# ─── CDR Dataclass ────────────────────────────────────────────────────────────

@dataclass
class CDR:
    """Call Detail Record for a single SIP call."""

    call_id: str = ""
    caller: str = ""
    callee: str = ""
    start_time: Optional[datetime.datetime] = None
    ring_time: Optional[datetime.datetime] = None
    answer_time: Optional[datetime.datetime] = None
    end_time: Optional[datetime.datetime] = None
    duration: float = 0.0            # total duration in seconds
    ring_duration: float = 0.0       # PDD / post-dial delay
    talk_duration: float = 0.0       # actual talk time
    status: CallStatus = CallStatus.INITIATED
    disconnect_cause: str = ""

    # SIP details
    sip_response_code: int = 0
    sip_method: str = "INVITE"
    sip_call_id: str = ""
    from_tag: str = ""
    to_tag: str = ""

    # Media / codec
    codec: str = ""
    bytes_sent: int = 0
    bytes_received: int = 0
    packets_sent: int = 0
    packets_received: int = 0
    packets_lost: int = 0

    # Quality metrics
    jitter: float = 0.0              # ms
    packet_loss: float = 0.0         # percentage 0-100
    r_factor: float = 0.0            # 0-100
    mos: float = 0.0                 # 1.0-5.0

    # Context
    scenario_name: str = ""
    test_id: str = ""
    local_ip: str = ""
    remote_ip: str = ""
    transport: str = "udp"

    def __post_init__(self):
        if not self.call_id:
            self.call_id = uuid.uuid4().hex[:16]
        if self.start_time is None:
            self.start_time = datetime.datetime.utcnow()

    def finalize(self) -> None:
        """Compute derived fields when the call ends."""
        if self.end_time is None:
            self.end_time = datetime.datetime.utcnow()
        if self.start_time:
            self.duration = (self.end_time - self.start_time).total_seconds()
        if self.ring_time and self.start_time:
            self.ring_duration = (self.ring_time - self.start_time).total_seconds()
        if self.answer_time and self.end_time:
            self.talk_duration = (self.end_time - self.answer_time).total_seconds()
        total = self.packets_sent + self.packets_received
        if total > 0:
            self.packet_loss = (self.packets_lost / total) * 100.0

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "caller": self.caller,
            "callee": self.callee,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "ring_time": self.ring_time.isoformat() if self.ring_time else None,
            "answer_time": self.answer_time.isoformat() if self.answer_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration": round(self.duration, 3),
            "ring_duration": round(self.ring_duration, 3),
            "talk_duration": round(self.talk_duration, 3),
            "status": self.status.value,
            "disconnect_cause": self.disconnect_cause,
            "sip_response_code": self.sip_response_code,
            "sip_method": self.sip_method,
            "sip_call_id": self.sip_call_id,
            "from_tag": self.from_tag,
            "to_tag": self.to_tag,
            "codec": self.codec,
            "bytes_sent": self.bytes_sent,
            "bytes_received": self.bytes_received,
            "packets_sent": self.packets_sent,
            "packets_received": self.packets_received,
            "packets_lost": self.packets_lost,
            "jitter": round(self.jitter, 3),
            "packet_loss": round(self.packet_loss, 2),
            "r_factor": round(self.r_factor, 1),
            "mos": round(self.mos, 2),
            "scenario_name": self.scenario_name,
            "test_id": self.test_id,
            "local_ip": self.local_ip,
            "remote_ip": self.remote_ip,
            "transport": self.transport,
        }


# ─── SQLAlchemy CDR Model ────────────────────────────────────────────────────

class CDRRecord(CDRBase):
    """Persistent CDR storage model."""
    __tablename__ = "cdr_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    call_id = Column(String(64), unique=True, nullable=False, index=True)
    caller = Column(String(255), default="", index=True)
    callee = Column(String(255), default="", index=True)
    start_time = Column(DateTime, nullable=True, index=True)
    ring_time = Column(DateTime, nullable=True)
    answer_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True, index=True)
    duration = Column(Float, default=0.0)
    ring_duration = Column(Float, default=0.0)
    talk_duration = Column(Float, default=0.0)
    status = Column(String(20), default="initiated", index=True)
    disconnect_cause = Column(String(255), default="")
    sip_response_code = Column(Integer, default=0, index=True)
    sip_method = Column(String(20), default="INVITE")
    sip_call_id = Column(String(255), default="")
    from_tag = Column(String(255), default="")
    to_tag = Column(String(255), default="")
    codec = Column(String(50), default="")
    bytes_sent = Column(Integer, default=0)
    bytes_received = Column(Integer, default=0)
    packets_sent = Column(Integer, default=0)
    packets_received = Column(Integer, default=0)
    packets_lost = Column(Integer, default=0)
    jitter = Column(Float, default=0.0)
    packet_loss = Column(Float, default=0.0)
    r_factor = Column(Float, default=0.0)
    mos = Column(Float, default=0.0)
    scenario_name = Column(String(255), default="", index=True)
    test_id = Column(String(255), default="", index=True)
    local_ip = Column(String(45), default="")
    remote_ip = Column(String(45), default="")
    transport = Column(String(10), default="udp")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Composite index for common queries
    __table_args__ = (
        Index("ix_cdr_start_status", "start_time", "status"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "call_id": self.call_id,
            "caller": self.caller,
            "callee": self.callee,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration": self.duration,
            "talk_duration": self.talk_duration,
            "status": self.status,
            "sip_response_code": self.sip_response_code,
            "codec": self.codec,
            "jitter": self.jitter,
            "packet_loss": self.packet_loss,
            "mos": self.mos,
            "scenario_name": self.scenario_name,
            "test_id": self.test_id,
        }


# ─── CDR Query Filter ────────────────────────────────────────────────────────

@dataclass
class CDRFilter:
    """Query filter for CDR searches."""

    start_after: Optional[datetime.datetime] = None
    start_before: Optional[datetime.datetime] = None
    caller: Optional[str] = None
    callee: Optional[str] = None
    status: Optional[str] = None
    sip_response_code: Optional[int] = None
    scenario_name: Optional[str] = None
    test_id: Optional[str] = None
    min_duration: Optional[float] = None
    max_duration: Optional[float] = None
    min_mos: Optional[float] = None
    limit: int = 100
    offset: int = 0

    def to_dict(self) -> dict:
        return {k: v for k, v in {
            "start_after": self.start_after.isoformat() if self.start_after else None,
            "start_before": self.start_before.isoformat() if self.start_before else None,
            "caller": self.caller,
            "callee": self.callee,
            "status": self.status,
            "sip_response_code": self.sip_response_code,
            "scenario_name": self.scenario_name,
            "test_id": self.test_id,
            "min_duration": self.min_duration,
            "max_duration": self.max_duration,
            "min_mos": self.min_mos,
            "limit": self.limit,
            "offset": self.offset,
        }.items() if v is not None}


# ─── Aggregate Stats ─────────────────────────────────────────────────────────

@dataclass
class CDRAggregateStats:
    """
    Telecom KPI aggregates.
    ASR  = Answer-Seizure Ratio (answered / total attempts) * 100
    ACD  = Average Call Duration (mean talk time of answered calls)
    NER  = Network Effectiveness Ratio ((answered + busy + no_answer) / total) * 100
    SDD  = Seizure-to-Disconnect Duration
    ABR  = Abandoned Before Ringing ratio
    """

    total_calls: int = 0
    answered_calls: int = 0
    failed_calls: int = 0
    busy_calls: int = 0
    no_answer_calls: int = 0
    cancelled_calls: int = 0
    timeout_calls: int = 0
    error_calls: int = 0

    total_duration: float = 0.0
    total_talk_duration: float = 0.0
    min_duration: float = 0.0
    max_duration: float = 0.0
    avg_duration: float = 0.0

    asr: float = 0.0          # Answer-Seizure Ratio
    acd: float = 0.0          # Average Call Duration
    ner: float = 0.0          # Network Effectiveness Ratio
    avg_pdd: float = 0.0      # Average Post-Dial Delay
    avg_jitter: float = 0.0
    avg_packet_loss: float = 0.0
    avg_mos: float = 0.0

    period_start: Optional[datetime.datetime] = None
    period_end: Optional[datetime.datetime] = None

    def to_dict(self) -> dict:
        return {
            "total_calls": self.total_calls,
            "answered_calls": self.answered_calls,
            "failed_calls": self.failed_calls,
            "busy_calls": self.busy_calls,
            "no_answer_calls": self.no_answer_calls,
            "cancelled_calls": self.cancelled_calls,
            "timeout_calls": self.timeout_calls,
            "error_calls": self.error_calls,
            "total_duration": round(self.total_duration, 2),
            "total_talk_duration": round(self.total_talk_duration, 2),
            "min_duration": round(self.min_duration, 3),
            "max_duration": round(self.max_duration, 3),
            "avg_duration": round(self.avg_duration, 3),
            "asr": round(self.asr, 2),
            "acd": round(self.acd, 2),
            "ner": round(self.ner, 2),
            "avg_pdd": round(self.avg_pdd, 3),
            "avg_jitter": round(self.avg_jitter, 3),
            "avg_packet_loss": round(self.avg_packet_loss, 2),
            "avg_mos": round(self.avg_mos, 2),
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
        }


# ─── CDR Store ────────────────────────────────────────────────────────────────

class CDRStore:
    """
    CDR storage engine with database persistence, real-time feed,
    querying, export, and aggregate statistics.
    """

    def __init__(self, config: Optional[Config] = None, db_url: Optional[str] = None):
        config = config or Config()
        self._db_url = db_url or config.db_url
        self._engine = create_engine(self._db_url, echo=False, pool_pre_ping=True)
        self._session_factory = sessionmaker(bind=self._engine)
        CDRBase.metadata.create_all(self._engine)

        self._lock = threading.Lock()
        self._live_buffer: deque[CDR] = deque(maxlen=1000)
        self._listeners: list[Callable[[CDR], Any]] = []

        logger.info("CDR store initialized (db=%s)", self._db_url.split("@")[-1] if "@" in self._db_url else self._db_url)

    def _get_session(self) -> Session:
        return self._session_factory()

    # ─── Write ────────────────────────────────────────────────────────────

    def record(self, cdr: CDR) -> None:
        """Persist a CDR and notify listeners."""
        cdr.finalize()

        session = self._get_session()
        try:
            record = CDRRecord(
                call_id=cdr.call_id,
                caller=cdr.caller,
                callee=cdr.callee,
                start_time=cdr.start_time,
                ring_time=cdr.ring_time,
                answer_time=cdr.answer_time,
                end_time=cdr.end_time,
                duration=cdr.duration,
                ring_duration=cdr.ring_duration,
                talk_duration=cdr.talk_duration,
                status=cdr.status.value,
                disconnect_cause=cdr.disconnect_cause,
                sip_response_code=cdr.sip_response_code,
                sip_method=cdr.sip_method,
                sip_call_id=cdr.sip_call_id,
                from_tag=cdr.from_tag,
                to_tag=cdr.to_tag,
                codec=cdr.codec,
                bytes_sent=cdr.bytes_sent,
                bytes_received=cdr.bytes_received,
                packets_sent=cdr.packets_sent,
                packets_received=cdr.packets_received,
                packets_lost=cdr.packets_lost,
                jitter=cdr.jitter,
                packet_loss=cdr.packet_loss,
                r_factor=cdr.r_factor,
                mos=cdr.mos,
                scenario_name=cdr.scenario_name,
                test_id=cdr.test_id,
                local_ip=cdr.local_ip,
                remote_ip=cdr.remote_ip,
                transport=cdr.transport,
            )
            session.add(record)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Failed to persist CDR %s", cdr.call_id)
            raise
        finally:
            session.close()

        # Live buffer + listeners
        with self._lock:
            self._live_buffer.append(cdr)
        for listener in self._listeners:
            try:
                listener(cdr)
            except Exception:
                logger.debug("CDR listener error", exc_info=True)

        logger.debug("CDR recorded: %s status=%s duration=%.1fs", cdr.call_id, cdr.status.value, cdr.duration)

    def record_batch(self, cdrs: list[CDR]) -> int:
        """Persist multiple CDRs in a single transaction. Returns count written."""
        session = self._get_session()
        count = 0
        try:
            for cdr in cdrs:
                cdr.finalize()
                record = CDRRecord(
                    call_id=cdr.call_id, caller=cdr.caller, callee=cdr.callee,
                    start_time=cdr.start_time, end_time=cdr.end_time,
                    duration=cdr.duration, talk_duration=cdr.talk_duration,
                    status=cdr.status.value, sip_response_code=cdr.sip_response_code,
                    codec=cdr.codec, bytes_sent=cdr.bytes_sent,
                    bytes_received=cdr.bytes_received, jitter=cdr.jitter,
                    packet_loss=cdr.packet_loss, mos=cdr.mos,
                    scenario_name=cdr.scenario_name, test_id=cdr.test_id,
                    local_ip=cdr.local_ip, remote_ip=cdr.remote_ip,
                    transport=cdr.transport,
                )
                session.add(record)
                count += 1
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Batch CDR write failed")
            raise
        finally:
            session.close()

        with self._lock:
            self._live_buffer.extend(cdrs)
        for cdr in cdrs:
            for listener in self._listeners:
                try:
                    listener(cdr)
                except Exception:
                    pass

        logger.info("Batch wrote %d CDRs", count)
        return count

    # ─── Read / Query ─────────────────────────────────────────────────────

    def get_by_call_id(self, call_id: str) -> Optional[dict]:
        session = self._get_session()
        try:
            rec = session.query(CDRRecord).filter_by(call_id=call_id).first()
            return rec.to_dict() if rec else None
        finally:
            session.close()

    def query(self, filt: CDRFilter) -> list[dict]:
        """Query CDRs with flexible filtering."""
        session = self._get_session()
        try:
            q = session.query(CDRRecord)
            if filt.start_after:
                q = q.filter(CDRRecord.start_time >= filt.start_after)
            if filt.start_before:
                q = q.filter(CDRRecord.start_time <= filt.start_before)
            if filt.caller:
                q = q.filter(CDRRecord.caller.ilike(f"%{filt.caller}%"))
            if filt.callee:
                q = q.filter(CDRRecord.callee.ilike(f"%{filt.callee}%"))
            if filt.status:
                q = q.filter(CDRRecord.status == filt.status)
            if filt.sip_response_code is not None:
                q = q.filter(CDRRecord.sip_response_code == filt.sip_response_code)
            if filt.scenario_name:
                q = q.filter(CDRRecord.scenario_name == filt.scenario_name)
            if filt.test_id:
                q = q.filter(CDRRecord.test_id == filt.test_id)
            if filt.min_duration is not None:
                q = q.filter(CDRRecord.duration >= filt.min_duration)
            if filt.max_duration is not None:
                q = q.filter(CDRRecord.duration <= filt.max_duration)
            if filt.min_mos is not None:
                q = q.filter(CDRRecord.mos >= filt.min_mos)

            q = q.order_by(CDRRecord.start_time.desc())
            q = q.offset(filt.offset).limit(filt.limit)
            return [r.to_dict() for r in q.all()]
        finally:
            session.close()

    def count(self, filt: Optional[CDRFilter] = None) -> int:
        session = self._get_session()
        try:
            q = session.query(CDRRecord)
            if filt:
                if filt.start_after:
                    q = q.filter(CDRRecord.start_time >= filt.start_after)
                if filt.start_before:
                    q = q.filter(CDRRecord.start_time <= filt.start_before)
                if filt.status:
                    q = q.filter(CDRRecord.status == filt.status)
            return q.count()
        finally:
            session.close()

    # ─── Live Feed ────────────────────────────────────────────────────────

    def add_listener(self, callback: Callable[[CDR], Any]) -> None:
        """Register a real-time CDR callback."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[CDR], Any]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def get_live_buffer(self, limit: int = 50) -> list[dict]:
        """Get the most recent CDRs from the in-memory ring buffer."""
        with self._lock:
            items = list(self._live_buffer)
        return [c.to_dict() for c in items[-limit:]]

    # ─── Aggregate Statistics ─────────────────────────────────────────────

    def aggregate(
        self,
        start: Optional[datetime.datetime] = None,
        end: Optional[datetime.datetime] = None,
        scenario_name: Optional[str] = None,
        test_id: Optional[str] = None,
    ) -> CDRAggregateStats:
        """Compute telecom KPIs over a time range."""
        session = self._get_session()
        try:
            q = session.query(CDRRecord)
            if start:
                q = q.filter(CDRRecord.start_time >= start)
            if end:
                q = q.filter(CDRRecord.start_time <= end)
            if scenario_name:
                q = q.filter(CDRRecord.scenario_name == scenario_name)
            if test_id:
                q = q.filter(CDRRecord.test_id == test_id)

            records = q.all()
            return self._compute_aggregates(records, start, end)
        finally:
            session.close()

    @staticmethod
    def _compute_aggregates(
        records: list[CDRRecord],
        period_start: Optional[datetime.datetime],
        period_end: Optional[datetime.datetime],
    ) -> CDRAggregateStats:
        stats = CDRAggregateStats(period_start=period_start, period_end=period_end)
        if not records:
            return stats

        stats.total_calls = len(records)
        durations: list[float] = []
        talk_durations: list[float] = []
        pdd_values: list[float] = []
        jitter_values: list[float] = []
        loss_values: list[float] = []
        mos_values: list[float] = []

        _status_map = {
            "completed": "answered_calls",
            "answered": "answered_calls",
            "failed": "failed_calls",
            "busy": "busy_calls",
            "no_answer": "no_answer_calls",
            "cancelled": "cancelled_calls",
            "timeout": "timeout_calls",
            "error": "error_calls",
        }

        for rec in records:
            attr = _status_map.get(rec.status)
            if attr:
                setattr(stats, attr, getattr(stats, attr) + 1)

            if rec.duration and rec.duration > 0:
                durations.append(rec.duration)
            if rec.talk_duration and rec.talk_duration > 0:
                talk_durations.append(rec.talk_duration)
            if rec.ring_duration and rec.ring_duration > 0:
                pdd_values.append(rec.ring_duration)
            if rec.jitter and rec.jitter > 0:
                jitter_values.append(rec.jitter)
            if rec.packet_loss is not None:
                loss_values.append(rec.packet_loss)
            if rec.mos and rec.mos > 0:
                mos_values.append(rec.mos)

        # Duration stats
        if durations:
            stats.total_duration = sum(durations)
            stats.min_duration = min(durations)
            stats.max_duration = max(durations)
            stats.avg_duration = stats.total_duration / len(durations)
        if talk_durations:
            stats.total_talk_duration = sum(talk_durations)

        # ASR = (answered / total) * 100
        if stats.total_calls > 0:
            stats.asr = (stats.answered_calls / stats.total_calls) * 100.0

        # ACD = average talk duration of answered calls
        if talk_durations:
            stats.acd = sum(talk_durations) / len(talk_durations)

        # NER = (answered + busy + no_answer) / total * 100
        effective = stats.answered_calls + stats.busy_calls + stats.no_answer_calls
        if stats.total_calls > 0:
            stats.ner = (effective / stats.total_calls) * 100.0

        # Averages
        if pdd_values:
            stats.avg_pdd = sum(pdd_values) / len(pdd_values)
        if jitter_values:
            stats.avg_jitter = sum(jitter_values) / len(jitter_values)
        if loss_values:
            stats.avg_packet_loss = sum(loss_values) / len(loss_values)
        if mos_values:
            stats.avg_mos = sum(mos_values) / len(mos_values)

        return stats

    # ─── Export ───────────────────────────────────────────────────────────

    def export_csv(
        self,
        filt: Optional[CDRFilter] = None,
        output_path: Optional[str] = None,
    ) -> str:
        """Export CDRs to CSV. Returns CSV as string; writes file if output_path given."""
        records = self.query(filt or CDRFilter(limit=10000))

        fieldnames = [
            "call_id", "caller", "callee", "start_time", "end_time",
            "duration", "talk_duration", "status", "sip_response_code",
            "codec", "bytes_sent", "bytes_received", "jitter", "packet_loss",
            "mos", "scenario_name", "test_id",
        ]

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)

        csv_data = buffer.getvalue()
        buffer.close()

        if output_path:
            with open(output_path, "w", newline="") as f:
                f.write(csv_data)
            logger.info("CDRs exported to CSV: %s (%d records)", output_path, len(records))

        return csv_data

    def export_json(
        self,
        filt: Optional[CDRFilter] = None,
        output_path: Optional[str] = None,
        indent: int = 2,
    ) -> str:
        """Export CDRs to JSON. Returns JSON string; writes file if output_path given."""
        records = self.query(filt or CDRFilter(limit=10000))
        payload = {
            "exported_at": datetime.datetime.utcnow().isoformat(),
            "count": len(records),
            "records": records,
        }
        json_data = json.dumps(payload, indent=indent, default=str)

        if output_path:
            with open(output_path, "w") as f:
                f.write(json_data)
            logger.info("CDRs exported to JSON: %s (%d records)", output_path, len(records))

        return json_data

    # ─── Maintenance ──────────────────────────────────────────────────────

    def purge(self, older_than: datetime.datetime) -> int:
        """Delete CDRs older than a given datetime. Returns count deleted."""
        session = self._get_session()
        try:
            count = session.query(CDRRecord).filter(
                CDRRecord.start_time < older_than
            ).delete()
            session.commit()
            logger.info("Purged %d CDRs older than %s", count, older_than.isoformat())
            return count
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def to_dict(self) -> dict:
        session = self._get_session()
        try:
            total = session.query(CDRRecord).count()
        finally:
            session.close()
        return {
            "total_records": total,
            "live_buffer_size": len(self._live_buffer),
            "listeners": len(self._listeners),
            "db_url": self._db_url.split("@")[-1] if "@" in self._db_url else self._db_url,
        }

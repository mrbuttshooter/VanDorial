"""
GenCall Job Scheduler.
Cron-like scheduling engine for recurring and one-shot SIP test jobs.
Supports calendar-aware scheduling, pause/resume, and API-friendly serialization.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("gencall.scheduler")

# ─── Constants ────────────────────────────────────────────────────────────────

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ─── Cron Expression Parser ──────────────────────────────────────────────────

class CronField:
    """Parses a single cron field (minute, hour, day-of-month, month, day-of-week)."""

    def __init__(self, expr: str, min_val: int, max_val: int):
        self.expr = expr
        self.min_val = min_val
        self.max_val = max_val
        self.values: set[int] = self._parse(expr)

    def _parse(self, expr: str) -> set[int]:
        values: set[int] = set()
        for part in expr.split(","):
            part = part.strip()
            if part == "*":
                values.update(range(self.min_val, self.max_val + 1))
            elif "/" in part:
                base, step_str = part.split("/", 1)
                step = int(step_str)
                if base == "*":
                    start = self.min_val
                else:
                    start = int(base)
                values.update(range(start, self.max_val + 1, step))
            elif "-" in part:
                lo, hi = part.split("-", 1)
                values.update(range(int(lo), int(hi) + 1))
            else:
                values.add(int(part))
        return values

    def matches(self, value: int) -> bool:
        return value in self.values


class CronExpression:
    """
    Parse and evaluate a 5-field cron expression.
    Format: minute hour day-of-month month day-of-week
    Examples:
        "*/30 * * * *"   -> every 30 minutes
        "0 9 * * 1-5"    -> 9 AM on weekdays
        "0 0 1 * *"      -> midnight on the 1st of each month
    """

    def __init__(self, expression: str):
        self.expression = expression.strip()
        parts = self.expression.split()
        if len(parts) != 5:
            raise ValueError(
                f"Cron expression must have 5 fields (got {len(parts)}): '{expression}'"
            )
        self.minute = CronField(parts[0], 0, 59)
        self.hour = CronField(parts[1], 0, 23)
        self.dom = CronField(parts[2], 1, 31)
        self.month = CronField(parts[3], 1, 12)
        self.dow = CronField(parts[4], 0, 6)  # 0=Monday in Python

    def matches(self, dt: datetime.datetime) -> bool:
        return (
            self.minute.matches(dt.minute)
            and self.hour.matches(dt.hour)
            and self.dom.matches(dt.day)
            and self.month.matches(dt.month)
            and self.dow.matches(dt.weekday())
        )

    def next_run(self, after: datetime.datetime) -> datetime.datetime:
        """Calculate the next datetime matching this cron expression after *after*."""
        # Start from the next whole minute
        candidate = after.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
        # Safety limit: scan up to 366 days ahead
        limit = candidate + datetime.timedelta(days=366)
        while candidate < limit:
            if self.matches(candidate):
                return candidate
            candidate += datetime.timedelta(minutes=1)
        raise RuntimeError(f"No matching time within 366 days for cron: {self.expression}")

    def __repr__(self) -> str:
        return f"CronExpression('{self.expression}')"


# ─── Calendar Helpers ─────────────────────────────────────────────────────────

class CalendarPolicy:
    """
    Calendar-aware scheduling policy.
    Can skip weekends, specific dates (holidays), and restrict to time windows.
    """

    def __init__(
        self,
        skip_weekends: bool = False,
        skip_dates: Optional[list[str]] = None,
        allowed_hours: Optional[tuple[int, int]] = None,
    ):
        self.skip_weekends = skip_weekends
        # Dates as "YYYY-MM-DD" strings for easy comparison
        self.skip_dates: set[str] = set(skip_dates or [])
        # Tuple of (start_hour, end_hour) inclusive; None means no restriction
        self.allowed_hours = allowed_hours

    def is_allowed(self, dt: datetime.datetime) -> bool:
        if self.skip_weekends and dt.weekday() >= 5:
            return False
        if dt.strftime("%Y-%m-%d") in self.skip_dates:
            return False
        if self.allowed_hours is not None:
            start_h, end_h = self.allowed_hours
            if not (start_h <= dt.hour <= end_h):
                return False
        return True

    def next_allowed(self, dt: datetime.datetime) -> datetime.datetime:
        """Advance *dt* to the next allowed moment."""
        limit = dt + datetime.timedelta(days=366)
        candidate = dt
        while candidate < limit:
            if self.is_allowed(candidate):
                return candidate
            candidate += datetime.timedelta(minutes=1)
        return dt  # Fallback: return original if nothing found

    def add_holiday(self, date_str: str) -> None:
        """Add a holiday in YYYY-MM-DD format."""
        self.skip_dates.add(date_str)

    def remove_holiday(self, date_str: str) -> None:
        self.skip_dates.discard(date_str)

    def to_dict(self) -> dict:
        return {
            "skip_weekends": self.skip_weekends,
            "skip_dates": sorted(self.skip_dates),
            "allowed_hours": list(self.allowed_hours) if self.allowed_hours else None,
        }


# ─── Job State ────────────────────────────────────────────────────────────────

class JobState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(Enum):
    CRON = "cron"
    INTERVAL = "interval"
    ONE_SHOT = "one_shot"


# ─── Job Dataclass ────────────────────────────────────────────────────────────

@dataclass
class ScheduledJob:
    """A scheduled test job."""

    job_id: str
    name: str
    job_type: JobType

    # What to run
    callback: Callable[..., Any] = field(repr=False)
    kwargs: dict[str, Any] = field(default_factory=dict)

    # Scheduling
    cron_expr: Optional[CronExpression] = field(default=None, repr=False)
    interval_seconds: float = 0.0
    delay_seconds: float = 0.0

    # Calendar policy
    calendar: Optional[CalendarPolicy] = None

    # State tracking
    state: JobState = JobState.PENDING
    next_run: Optional[datetime.datetime] = None
    last_run: Optional[datetime.datetime] = None
    last_result: Optional[str] = None
    last_error: Optional[str] = None
    run_count: int = 0
    error_count: int = 0
    max_runs: int = 0  # 0 = unlimited

    # Metadata
    created_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    tags: list[str] = field(default_factory=list)

    def _compute_next_run(self, now: datetime.datetime) -> Optional[datetime.datetime]:
        """Calculate when this job should run next."""
        if self.job_type == JobType.ONE_SHOT:
            if self.run_count == 0:
                candidate = now + datetime.timedelta(seconds=self.delay_seconds)
            else:
                return None  # One-shot already fired
        elif self.job_type == JobType.INTERVAL:
            if self.last_run:
                candidate = self.last_run + datetime.timedelta(seconds=self.interval_seconds)
            else:
                candidate = now + datetime.timedelta(seconds=self.delay_seconds)
        elif self.job_type == JobType.CRON:
            if self.cron_expr is None:
                return None
            candidate = self.cron_expr.next_run(now)
        else:
            return None

        # Apply calendar policy
        if self.calendar and candidate:
            candidate = self.calendar.next_allowed(candidate)

        return candidate

    def schedule_next(self, now: Optional[datetime.datetime] = None) -> None:
        now = now or datetime.datetime.utcnow()
        if self.max_runs > 0 and self.run_count >= self.max_runs:
            self.state = JobState.COMPLETED
            self.next_run = None
            return
        self.next_run = self._compute_next_run(now)
        if self.next_run is None:
            self.state = JobState.COMPLETED

    def is_due(self, now: Optional[datetime.datetime] = None) -> bool:
        now = now or datetime.datetime.utcnow()
        if self.state != JobState.PENDING:
            return False
        if self.next_run is None:
            return False
        return now >= self.next_run

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "job_type": self.job_type.value,
            "state": self.state.value,
            "cron_expression": self.cron_expr.expression if self.cron_expr else None,
            "interval_seconds": self.interval_seconds,
            "delay_seconds": self.delay_seconds,
            "calendar_policy": self.calendar.to_dict() if self.calendar else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "max_runs": self.max_runs,
            "created_at": self.created_at.isoformat(),
            "tags": self.tags,
            "kwargs": {k: str(v) for k, v in self.kwargs.items()},
        }


# ─── Job History Entry ────────────────────────────────────────────────────────

@dataclass
class JobExecution:
    """Record of a single job execution."""

    job_id: str
    job_name: str
    started_at: datetime.datetime
    finished_at: Optional[datetime.datetime] = None
    duration_seconds: float = 0.0
    success: bool = False
    result: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "job_name": self.job_name,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": round(self.duration_seconds, 3),
            "success": self.success,
            "result": self.result,
            "error": self.error,
        }


# ─── Scheduler Engine ─────────────────────────────────────────────────────────

def _generate_job_id(name: str) -> str:
    ts = str(time.monotonic_ns())
    return hashlib.sha256(f"{name}:{ts}".encode()).hexdigest()[:12]


class Scheduler:
    """
    Thread-based job scheduler.
    Runs a background tick loop that checks for due jobs and executes them
    in worker threads from a pool.
    """

    def __init__(
        self,
        tick_interval: float = 1.0,
        max_workers: int = 8,
        history_limit: int = 500,
    ):
        self._tick_interval = tick_interval
        self._max_workers = max_workers
        self._history_limit = history_limit

        self._jobs: dict[str, ScheduledJob] = {}
        self._lock = threading.RLock()
        self._running = False
        self._tick_thread: Optional[threading.Thread] = None
        self._worker_semaphore = threading.Semaphore(max_workers)
        self._history: list[JobExecution] = []
        self._listeners: list[Callable[[JobExecution], Any]] = []

        logger.info(
            "Scheduler initialized (tick=%.1fs, workers=%d)", tick_interval, max_workers
        )

    # ─── Job Registration ─────────────────────────────────────────────────

    def add_cron_job(
        self,
        name: str,
        cron_expression: str,
        callback: Callable[..., Any],
        kwargs: Optional[dict[str, Any]] = None,
        calendar: Optional[CalendarPolicy] = None,
        max_runs: int = 0,
        tags: Optional[list[str]] = None,
    ) -> ScheduledJob:
        """Schedule a job with a cron expression."""
        cron = CronExpression(cron_expression)
        job = ScheduledJob(
            job_id=_generate_job_id(name),
            name=name,
            job_type=JobType.CRON,
            callback=callback,
            kwargs=kwargs or {},
            cron_expr=cron,
            calendar=calendar,
            max_runs=max_runs,
            tags=tags or [],
        )
        job.schedule_next()
        return self._register_job(job)

    def add_interval_job(
        self,
        name: str,
        interval_seconds: float,
        callback: Callable[..., Any],
        kwargs: Optional[dict[str, Any]] = None,
        initial_delay: float = 0.0,
        calendar: Optional[CalendarPolicy] = None,
        max_runs: int = 0,
        tags: Optional[list[str]] = None,
    ) -> ScheduledJob:
        """Schedule a job to run at a fixed interval."""
        job = ScheduledJob(
            job_id=_generate_job_id(name),
            name=name,
            job_type=JobType.INTERVAL,
            callback=callback,
            kwargs=kwargs or {},
            interval_seconds=interval_seconds,
            delay_seconds=initial_delay,
            calendar=calendar,
            max_runs=max_runs,
            tags=tags or [],
        )
        job.schedule_next()
        return self._register_job(job)

    def add_one_shot_job(
        self,
        name: str,
        delay_seconds: float,
        callback: Callable[..., Any],
        kwargs: Optional[dict[str, Any]] = None,
        tags: Optional[list[str]] = None,
    ) -> ScheduledJob:
        """Schedule a single execution after a delay."""
        job = ScheduledJob(
            job_id=_generate_job_id(name),
            name=name,
            job_type=JobType.ONE_SHOT,
            callback=callback,
            kwargs=kwargs or {},
            delay_seconds=delay_seconds,
            max_runs=1,
            tags=tags or [],
        )
        job.schedule_next()
        return self._register_job(job)

    def _register_job(self, job: ScheduledJob) -> ScheduledJob:
        with self._lock:
            self._jobs[job.job_id] = job
        logger.info(
            "Job registered: id=%s name='%s' type=%s next_run=%s",
            job.job_id,
            job.name,
            job.job_type.value,
            job.next_run,
        )
        return job

    # ─── Job Control ──────────────────────────────────────────────────────

    def pause_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.state not in (JobState.PENDING, JobState.RUNNING):
                return False
            job.state = JobState.PAUSED
            logger.info("Job paused: %s (%s)", job.name, job_id)
            return True

    def resume_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.state != JobState.PAUSED:
                return False
            job.state = JobState.PENDING
            job.schedule_next()
            logger.info("Job resumed: %s (%s)", job.name, job_id)
            return True

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.state = JobState.CANCELLED
            job.next_run = None
            logger.info("Job cancelled: %s (%s)", job.name, job_id)
            return True

    def remove_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if not job:
                return False
            job.state = JobState.CANCELLED
            logger.info("Job removed: %s (%s)", job.name, job_id)
            return True

    def get_job(self, job_id: str) -> Optional[ScheduledJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, state: Optional[JobState] = None, tag: Optional[str] = None) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        if state is not None:
            jobs = [j for j in jobs if j.state == state]
        if tag is not None:
            jobs = [j for j in jobs if tag in j.tags]
        return [j.to_dict() for j in jobs]

    # ─── Scheduler Lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            logger.warning("Scheduler already running")
            return
        self._running = True
        self._tick_thread = threading.Thread(
            target=self._tick_loop, daemon=True, name="scheduler-tick"
        )
        self._tick_thread.start()
        logger.info("Scheduler started")

    def stop(self, wait: bool = True) -> None:
        if not self._running:
            return
        self._running = False
        if wait and self._tick_thread:
            self._tick_thread.join(timeout=self._tick_interval * 3)
        logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ─── Execution History ────────────────────────────────────────────────

    def get_history(
        self,
        job_id: Optional[str] = None,
        limit: int = 50,
        success_only: Optional[bool] = None,
    ) -> list[dict]:
        with self._lock:
            history = list(self._history)
        if job_id:
            history = [h for h in history if h.job_id == job_id]
        if success_only is not None:
            history = [h for h in history if h.success == success_only]
        return [h.to_dict() for h in history[-limit:]]

    def add_listener(self, callback: Callable[[JobExecution], Any]) -> None:
        """Register a callback invoked after every job execution."""
        self._listeners.append(callback)

    # ─── Internal Tick Loop ───────────────────────────────────────────────

    def _tick_loop(self) -> None:
        logger.debug("Scheduler tick loop started")
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("Scheduler tick error")
            time.sleep(self._tick_interval)
        logger.debug("Scheduler tick loop exited")

    def _tick(self) -> None:
        now = datetime.datetime.utcnow()
        due_jobs: list[ScheduledJob] = []

        with self._lock:
            for job in self._jobs.values():
                if job.is_due(now):
                    due_jobs.append(job)

        for job in due_jobs:
            self._dispatch(job)

    def _dispatch(self, job: ScheduledJob) -> None:
        """Dispatch a job for execution in a worker thread."""
        acquired = self._worker_semaphore.acquire(blocking=False)
        if not acquired:
            logger.warning(
                "All worker slots busy, deferring job %s (%s)", job.name, job.job_id
            )
            return

        with self._lock:
            job.state = JobState.RUNNING

        worker = threading.Thread(
            target=self._execute_job,
            args=(job,),
            daemon=True,
            name=f"scheduler-worker-{job.job_id[:8]}",
        )
        worker.start()

    def _execute_job(self, job: ScheduledJob) -> None:
        execution = JobExecution(
            job_id=job.job_id,
            job_name=job.name,
            started_at=datetime.datetime.utcnow(),
        )
        try:
            logger.info("Executing job: %s (%s)", job.name, job.job_id)
            result = job.callback(**job.kwargs)
            execution.success = True
            execution.result = str(result) if result is not None else None
            job.last_result = execution.result
            job.last_error = None
        except Exception as exc:
            execution.success = False
            execution.error = str(exc)
            job.last_error = str(exc)
            job.error_count += 1
            logger.exception("Job %s failed: %s", job.name, exc)
        finally:
            self._worker_semaphore.release()
            finished = datetime.datetime.utcnow()
            execution.finished_at = finished
            execution.duration_seconds = (
                finished - execution.started_at
            ).total_seconds()

            job.run_count += 1
            job.last_run = finished

            with self._lock:
                # Reschedule
                if job.state == JobState.RUNNING:
                    job.state = JobState.PENDING
                    job.schedule_next(finished)

                # Store history
                self._history.append(execution)
                if len(self._history) > self._history_limit:
                    self._history = self._history[-self._history_limit:]

            # Notify listeners
            for listener in self._listeners:
                try:
                    listener(execution)
                except Exception:
                    logger.debug("Scheduler listener error", exc_info=True)

    # ─── Convenience / API ────────────────────────────────────────────────

    def to_dict(self) -> dict:
        with self._lock:
            jobs = [j.to_dict() for j in self._jobs.values()]
            pending = sum(1 for j in self._jobs.values() if j.state == JobState.PENDING)
            running = sum(1 for j in self._jobs.values() if j.state == JobState.RUNNING)
            paused = sum(1 for j in self._jobs.values() if j.state == JobState.PAUSED)
        return {
            "running": self._running,
            "total_jobs": len(jobs),
            "pending": pending,
            "active": running,
            "paused": paused,
            "max_workers": self._max_workers,
            "tick_interval": self._tick_interval,
            "history_size": len(self._history),
            "jobs": jobs,
        }

"""
GenCall SIPp Engine - Controls SIPp processes for SIP traffic generation.
Manages launching, monitoring, and stopping SIPp instances.
"""

import subprocess
import os
import signal
import time
import threading
import logging
import shlex
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from gencall.core.config import Config

logger = logging.getLogger("gencall.sipp")

# os.setsid / os.killpg / os.getpgid are POSIX-only. On Windows (and any other
# non-POSIX platform) they are absent, so we detect support once and fall back to
# plain process control there. This keeps Unix behavior (start SIPp in its own
# session/process group so we can signal the whole group) unchanged.
_HAS_SETSID = hasattr(os, "setsid") and hasattr(os, "killpg") and hasattr(os, "getpgid")

# SIPp ResponseTime columns we read, in preference order: cumulative average
# first (matches the dashboard's "avg" semantics), then the periodic value.
_RESPONSE_TIME_COLUMNS = ("ResponseTime1(C)", "ResponseTime1(P)",
                          "ResponseTime(C)", "ResponseTime(P)")


def _parse_response_time_ms(stats_dict):
    """Parse SIPp's ResponseTime column to milliseconds, or None if absent.

    SIPp formats a ResponseTime as ``HH:MM:SS:mmm`` (colon-separated, the last
    field milliseconds) — e.g. ``00:00:00:042`` is 42 ms. Some builds emit a
    plain numeric seconds value instead. We accept both and return ms as a float;
    a missing or unparseable column yields None so the caller leaves the field
    unchanged (never fabricating a number SIPp didn't report).
    """
    raw = None
    for col in _RESPONSE_TIME_COLUMNS:
        if stats_dict.get(col):
            raw = stats_dict[col].strip()
            break
    if not raw:
        return None
    if ":" in raw:
        # HH:MM:SS:mmm (milliseconds in the final field).
        parts = raw.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        if len(nums) == 4:
            hh, mm, ss, mmm = nums
            return ((hh * 3600 + mm * 60 + ss) * 1000) + mmm
        if len(nums) == 3:
            hh, mm, ss = nums
            return (hh * 3600 + mm * 60 + ss) * 1000.0
        return None
    # Plain numeric: SIPp reports seconds; convert to ms.
    try:
        return float(raw) * 1000.0
    except ValueError:
        return None


class SIPpTransport(Enum):
    UDP = "u1"
    TCP = "t1"
    TLS = "l1"


class SIPpMode(Enum):
    UAC = "uac"  # client - makes calls
    UAS = "uas"  # server - receives calls


class SIPpState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class SIPpStats:
    """Real-time stats from a running SIPp instance."""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    current_calls: int = 0
    retransmissions: int = 0
    calls_per_second: float = 0.0
    avg_response_time_ms: float = 0.0
    start_time: float = 0.0

    @property
    def uptime(self):
        if self.start_time:
            return time.time() - self.start_time
        return 0.0

    @property
    def success_rate(self):
        total = self.successful_calls + self.failed_calls
        if total == 0:
            return 0.0
        return (self.successful_calls / total) * 100

    def to_dict(self):
        return {
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "current_calls": self.current_calls,
            "retransmissions": self.retransmissions,
            "calls_per_second": round(self.calls_per_second, 2),
            "avg_response_time_ms": round(self.avg_response_time_ms, 2),
            "uptime_seconds": round(self.uptime, 1),
            "success_rate": round(self.success_rate, 2),
        }


@dataclass
class SIPpInstance:
    """Represents a single SIPp process."""
    id: str
    scenario_file: str
    remote_host: str
    remote_port: int = 5060
    local_port: int = 5060
    local_ip: str = ""
    mode: SIPpMode = SIPpMode.UAC
    transport: SIPpTransport = SIPpTransport.UDP
    call_rate: float = 1.0
    max_calls: int = 0  # 0 = unlimited
    call_limit: int = 10  # concurrent call limit
    duration: int = 0  # 0 = run forever
    csv_file: str = ""
    auth_user: str = ""
    auth_pass: str = ""
    extra_args: str = ""
    campaign_id: str = ""  # owning loop campaign (§4.5), "" for one-shot tests
    state: SIPpState = SIPpState.IDLE
    stats: SIPpStats = field(default_factory=SIPpStats)
    _process: Optional[subprocess.Popen] = field(default=None, repr=False)
    _monitor_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _stats_file: str = field(default="", repr=False)
    error_message: str = ""

    def build_command(self, config: Config) -> list:
        """Build the SIPp command line arguments."""
        sipp_bin = config.sipp_command
        cmd = [sipp_bin]

        # Scenario
        cmd.extend(["-sf", self.scenario_file])

        # Remote target
        cmd.append(f"{self.remote_host}:{self.remote_port}")

        # Local binding. -i sets the SIP signalling source address; -mi sets the
        # media (RTP) source address. We bind both to local_ip when known so the
        # SDP we advertise ([media_ip]) matches the socket SIPp actually echoes
        # on — otherwise -rtp_echo and the SDP can land on the wrong interface.
        if self.local_ip:
            cmd.extend(["-i", self.local_ip])
            cmd.extend(["-mi", self.local_ip])
        cmd.extend(["-p", str(self.local_port)])

        # RTP media port window. ALWAYS pin SIPp's media ports inside the
        # firewalled range (config [sip] min/max_rtp_port) — without this SIPp
        # uses its built-in ~6000 base, which the host firewall drops, so return
        # media never arrives. min < max is validated in Config (warn-only); we
        # emit whatever is configured so the window is explicit on every launch.
        cmd.extend([
            "-min_rtp_port", str(config.min_rtp_port),
            "-max_rtp_port", str(config.max_rtp_port),
        ])

        # Transport
        cmd.extend(["-t", self.transport.value])

        # Call rate and limits
        cmd.extend(["-r", str(self.call_rate)])
        if self.max_calls > 0:
            cmd.extend(["-m", str(self.max_calls)])
        cmd.extend(["-l", str(self.call_limit)])

        # Duration
        if self.duration > 0:
            cmd.extend(["-d", str(self.duration * 1000)])  # ms

        # CSV injection file
        if self.csv_file:
            cmd.extend(["-inf", self.csv_file])

        # Authentication
        if self.auth_user:
            cmd.extend(["-au", self.auth_user])
        if self.auth_pass:
            cmd.extend(["-ap", self.auth_pass])

        # Stats output. The directory comes from config ([sipp] stats_dir,
        # default /tmp) so Linux behavior is unchanged, but /tmp is POSIX-only
        # and absent on Windows — fall back to the platform temp dir if the
        # configured location does not exist and cannot be created.
        stats_dir = config.sipp_stats_dir or tempfile.gettempdir()
        if not os.path.isdir(stats_dir):
            try:
                os.makedirs(stats_dir, exist_ok=True)
            except OSError:
                stats_dir = tempfile.gettempdir()
        self._stats_file = os.path.join(stats_dir, f"gencall_sipp_{self.id}.csv")
        cmd.extend(["-trace_stat", "-stf", self._stats_file, "-fd", "1"])

        # Screen output control. -trace_err writes SIPp's own error file, so we
        # do NOT need (and must not keep) a stderr PIPE open — see start_instance,
        # which routes stderr to DEVNULL to avoid a full-pipe deadlock on a busy
        # run. We run SIPp in the FOREGROUND (no -bg): -bg daemonizes (forks and
        # the parent exits immediately), which would leave Popen tracking a dead
        # parent while the real dialer runs as an untracked orphan. Staying in the
        # foreground under our os.setsid process group keeps the spawned PID the
        # one we signal/reap and the one recorded for crash-orphan reconciliation.
        cmd.extend(["-trace_err", "-trace_logs"])

        # Extra args
        if self.extra_args:
            cmd.extend(shlex.split(self.extra_args))

        return cmd

    def to_dict(self):
        return {
            "id": self.id,
            "scenario_file": self.scenario_file,
            "remote_host": self.remote_host,
            "remote_port": self.remote_port,
            "local_port": self.local_port,
            "local_ip": self.local_ip,
            "mode": self.mode.value,
            "transport": self.transport.value,
            "call_rate": self.call_rate,
            "max_calls": self.max_calls,
            "call_limit": self.call_limit,
            "duration": self.duration,
            "state": self.state.value,
            "stats": self.stats.to_dict(),
            "error_message": self.error_message,
        }


class SIPpEngine:
    """
    Manages multiple SIPp instances.
    Handles launching, monitoring, and stopping SIPp processes.
    """

    def __init__(self, config: Config = None, registry=None):
        self.config = config or Config()
        self.instances: dict[str, SIPpInstance] = {}
        # ProcessRegistry (gencall.core.process_registry) records every spawned
        # SIPp PID for crash-orphan reconciliation (design §4.5). Optional so the
        # engine still runs standalone (and in tests) without a registry wired.
        self.registry = registry
        self._lock = threading.Lock()
        self._set_file_limit()

    def _set_file_limit(self):
        """Set the open file limit for SIPp."""
        try:
            import resource
            limit = self.config.sipp_file_limit
            resource.setrlimit(resource.RLIMIT_NOFILE, (limit, limit))
        except (ImportError, ValueError, OSError) as e:
            logger.warning("Could not set file limit: %s", e)

    def start_instance(self, instance: SIPpInstance) -> bool:
        """Start a SIPp instance."""
        with self._lock:
            if instance.id in self.instances:
                existing = self.instances[instance.id]
                if existing.state in (SIPpState.RUNNING, SIPpState.STARTING):
                    logger.warning("Instance %s is already %s",
                                   instance.id, existing.state.value)
                    return False

            self.instances[instance.id] = instance
            # Set STARTING synchronously while we still hold the engine lock so a
            # concurrent monitor pass (LoopEngine restart check) sees this
            # instance as not-dead the instant it is registered — preventing a
            # double-launch race for the SIP port (design §8).
            instance.state = SIPpState.STARTING

        try:
            cmd = instance.build_command(self.config)
            logger.info("Starting SIPp: %s", " ".join(cmd))

            popen_kwargs = {
                "stdout": subprocess.DEVNULL,
                # SIPp writes its own error file via -trace_err; an unread stderr
                # PIPE can fill its kernel buffer and deadlock a busy run, so we
                # discard it here rather than holding a pipe we never drain.
                "stderr": subprocess.DEVNULL,
            }
            if _HAS_SETSID:
                # POSIX: start SIPp in a new session so we can later signal the
                # whole process group (graceful SIGUSR1, then SIGKILL).
                popen_kwargs["preexec_fn"] = os.setsid
            instance._process = subprocess.Popen(cmd, **popen_kwargs)

            # Give it a moment to start. SIPp now runs in the FOREGROUND (no -bg),
            # so a still-alive child after this poll is the real dialer — not a
            # forked-and-exited daemon parent. An early exit here means a real
            # startup failure (bad scenario, port in use, ...); SIPp's own
            # -trace_err file holds the detail.
            time.sleep(0.5)
            if instance._process.poll() is not None:
                exit_code = instance._process.returncode
                instance.state = SIPpState.ERROR
                instance.error_message = (
                    f"SIPp exited during startup (code {exit_code}); "
                    f"see SIPp -trace_err file"
                )
                logger.error("SIPp failed to start (exit %s): %s",
                             exit_code, instance.error_message)
                return False

            instance.state = SIPpState.RUNNING
            instance.stats.start_time = time.time()

            # Register the spawned PID for crash-orphan reconciliation (§4.5).
            # We hash the exact argv so a recycled PID running something else is
            # distinguishable at reconcile time. Best-effort: a registry failure
            # must never stop a working SIPp from running.
            if self.registry is not None:
                try:
                    from gencall.core.process_registry import (
                        cmdline_hash,
                        current_cmdline_hash,
                    )

                    pid = instance._process.pid
                    # Prefer the OS-reported live cmdline so reconciliation
                    # compares like-for-like; fall back to the argv we launched
                    # if the OS won't report it (locked-down host).
                    h = current_cmdline_hash(pid) or cmdline_hash(cmd)
                    self.registry.record(
                        pid=pid,
                        role=instance.mode.value,
                        cmdline_hash_value=h,
                        campaign_id=instance.campaign_id or None,
                    )
                except Exception as e:
                    logger.warning("Could not register PID for %s: %s", instance.id, e)

            # Start stats monitor thread
            instance._monitor_thread = threading.Thread(
                target=self._monitor_instance,
                args=(instance,),
                daemon=True,
                name=f"sipp-monitor-{instance.id}",
            )
            instance._monitor_thread.start()

            logger.info("SIPp instance %s started (PID %d)", instance.id, instance._process.pid)
            return True

        except FileNotFoundError:
            instance.state = SIPpState.ERROR
            instance.error_message = f"SIPp binary not found: {self.config.sipp_command}"
            logger.error(instance.error_message)
            return False
        except Exception as e:
            instance.state = SIPpState.ERROR
            instance.error_message = str(e)
            logger.exception("Failed to start SIPp instance %s", instance.id)
            return False

    def stop_instance(self, instance_id: str) -> bool:
        """Gracefully stop a SIPp instance.

        The whole body runs under the engine lock so a concurrent monitor pass
        (LoopEngine's UAS restart check) can never observe a half-stopped
        instance and double-launch a replacement fighting for the SIP port
        (design §8). A STARTING instance is stoppable too — start_instance sets
        STARTING synchronously under this same lock, so by the time we hold it the
        process either exists (and we signal it) or the start already failed.
        """
        with self._lock:
            instance = self.instances.get(instance_id)
            if not instance:
                return False

            if instance.state not in (SIPpState.RUNNING, SIPpState.STARTING):
                return False

            instance.state = SIPpState.STOPPING
            stopped_pid = instance._process.pid if instance._process else None
            try:
                if instance._process and instance._process.poll() is None:
                    if _HAS_SETSID:
                        # POSIX: signal the whole process group. SIGUSR1 is SIPp's
                        # convention for a graceful drain-and-exit.
                        pgid = os.getpgid(instance._process.pid)
                        os.killpg(pgid, signal.SIGUSR1)
                        try:
                            instance._process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            # Force kill if graceful shutdown failed
                            os.killpg(pgid, signal.SIGKILL)
                            instance._process.wait(timeout=5)
                    else:
                        # Non-POSIX (e.g. Windows): no process groups / SIGUSR1.
                        # Fall back to terminate(), escalating to kill() on timeout.
                        instance._process.terminate()
                        try:
                            instance._process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            instance._process.kill()
                            instance._process.wait(timeout=5)

                instance.state = SIPpState.STOPPED
                # The PID is gone — forget it so reconciliation never targets it.
                if self.registry is not None and stopped_pid is not None:
                    try:
                        self.registry.clear(stopped_pid)
                    except Exception as e:
                        logger.debug("Registry clear of PID %s failed: %s", stopped_pid, e)
                logger.info("SIPp instance %s stopped", instance_id)
                return True
            except Exception as e:
                instance.state = SIPpState.ERROR
                instance.error_message = str(e)
                logger.exception("Error stopping SIPp instance %s", instance_id)
                return False

    def stop_all(self):
        """Stop all running instances."""
        for instance_id in list(self.instances.keys()):
            self.stop_instance(instance_id)

    def remove_instance(self, instance_id: str) -> bool:
        """Remove a stopped instance."""
        with self._lock:
            instance = self.instances.get(instance_id)
            if not instance:
                return False
            if instance.state == SIPpState.RUNNING:
                return False
            # Cleanup stats file
            if instance._stats_file and os.path.exists(instance._stats_file):
                os.remove(instance._stats_file)
            del self.instances[instance_id]
            return True

    def get_instance(self, instance_id: str) -> Optional[SIPpInstance]:
        return self.instances.get(instance_id)

    def list_instances(self) -> list[dict]:
        return [inst.to_dict() for inst in self.instances.values()]

    def update_call_rate(self, instance_id: str, new_rate: float) -> bool:
        """Dynamically update the call rate of a running instance."""
        instance = self.instances.get(instance_id)
        if not instance or instance.state != SIPpState.RUNNING:
            return False
        # SIPp supports dynamic rate change via key commands on stdin
        # In background mode, we'd need to restart - or use the remote control port
        instance.call_rate = new_rate
        logger.info("Call rate for %s updated to %f", instance_id, new_rate)
        return True

    def _monitor_instance(self, instance: SIPpInstance):
        """Monitor a SIPp instance by reading its stats file."""
        while instance.state == SIPpState.RUNNING:
            try:
                if instance._process and instance._process.poll() is not None:
                    exit_code = instance._process.returncode
                    # Read the final stats row before breaking: a short finite
                    # (-m) run can exit between poll intervals, so without this
                    # last read its end-state counters would never be ingested.
                    try:
                        self._read_stats(instance)
                    except Exception:
                        pass
                    # The process has exited on its own — forget its PID so a
                    # later reconciliation never targets a now-dead/recycled PID.
                    if self.registry is not None:
                        try:
                            self.registry.clear(instance._process.pid)
                        except Exception:
                            pass
                    if exit_code == 0:
                        instance.state = SIPpState.STOPPED
                        logger.info("SIPp instance %s completed normally", instance.id)
                    else:
                        # stderr is DEVNULL (not piped) — SIPp records the detail
                        # in its own -trace_err file, so we just report the code.
                        instance.state = SIPpState.ERROR
                        instance.error_message = (
                            f"Exit code {exit_code}; see SIPp -trace_err file"
                        )
                        logger.error("SIPp instance %s exited with code %d", instance.id, exit_code)
                    break

                self._read_stats(instance)
            except Exception as e:
                logger.debug("Stats read error for %s: %s", instance.id, e)

            time.sleep(self.config.stats_interval)

    def _read_stats(self, instance: SIPpInstance):
        """Parse SIPp CSV stats file."""
        if not instance._stats_file or not os.path.exists(instance._stats_file):
            return

        try:
            with open(instance._stats_file, "r") as f:
                lines = f.readlines()
                if len(lines) < 2:
                    return
                # SIPp stats CSV format - last line has current stats
                headers = lines[0].strip().split(";")
                values = lines[-1].strip().split(";")

                if len(headers) != len(values):
                    return

                stats_dict = dict(zip(headers, values))

                instance.stats.total_calls = int(stats_dict.get("TotalCallCreated", 0))
                instance.stats.successful_calls = int(stats_dict.get("SuccessfulCall(C)", 0))
                instance.stats.failed_calls = int(stats_dict.get("FailedCall(C)", 0))
                instance.stats.current_calls = int(stats_dict.get("CurrentCall", 0))
                instance.stats.retransmissions = int(stats_dict.get("Retransmissions(C)", 0))

                # Real average response time, when SIPp reports it. SIPp emits a
                # cumulative ResponseTime column (a <responsetime>-paired scenario
                # gives ResponseTime1(C)); older/unconfigured runs omit it. Parse
                # it honestly into ms — when the column is absent the value stays
                # at its prior value (0 on a run that never reports it) rather than
                # the old dishonest constant 0 the metric used to be.
                rt = _parse_response_time_ms(stats_dict)
                if rt is not None:
                    instance.stats.avg_response_time_ms = rt

                elapsed = instance.stats.uptime
                if elapsed > 0:
                    instance.stats.calls_per_second = instance.stats.total_calls / elapsed

        except (ValueError, KeyError, IndexError):
            pass

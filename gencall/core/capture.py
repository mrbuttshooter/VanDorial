"""On-demand pcap capture: run/track tcpdump per loop, with a size/duration
watchdog. DB-free; the API layer resolves a campaign's ports and calls in here."""
import logging
import os
import shlex
import signal
import subprocess
import threading
import time
import uuid

logger = logging.getLogger("gencall.capture")

# os.setsid / os.killpg / os.getpgid are POSIX-only (mirrors sipp_engine). On
# Windows they are absent, so we detect support once and fall back to plain
# process control. tcpdump itself is Linux-only — on Windows the start() call
# fails cleanly (RuntimeError) and the API turns that into a 503.
_HAS_SETSID = hasattr(os, "setsid") and hasattr(os, "killpg") and hasattr(os, "getpgid")


def build_capture_filter(dest_host, dest_port=5060, local_port=0,
                         media_port=0, transport="udp") -> str:
    """A BPF filter scoping the capture to one loop: signalling + RTP to/from the
    destination switch. tcpdump is run on 'any' iface, so we filter by host+ports."""
    proto = "tcp" if str(transport).lower().startswith("t") else "udp"
    parts = []
    if dest_host:
        parts.append(f"host {dest_host}")
    ports = []
    if dest_port:
        ports.append(f"{proto} port {dest_port}")
    if local_port:
        ports.append(f"{proto} port {local_port}")
    if media_port:
        # RTP (p), RTCP (p+1), and SIPp's -rtp_echo mirror (p+2).
        ports.append(f"udp portrange {media_port}-{media_port + 2}")
    expr = ""
    if parts:
        expr = parts[0]
    if ports:
        port_expr = "(" + " or ".join(ports) + ")"
        expr = f"{expr} and {port_expr}" if expr else port_expr
    return expr or proto


class _Capture:
    def __init__(self, cap_id, campaign_id, path, proc):
        self.id = cap_id
        self.campaign_id = campaign_id
        self.path = path
        self.proc = proc
        self.started_at = None  # epoch; set by the manager right after start
        self.stopped_at = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None


class CaptureManager:
    """Starts/stops/tracks tcpdump captures. One watchdog thread enforces the
    size/duration caps across all captures."""

    def __init__(self, command="tcpdump", capture_dir="/tmp",
                 max_seconds=300, max_mb=100, snaplen=0):
        self._command = command
        self._dir = capture_dir
        self._max_seconds = int(max_seconds)
        self._max_bytes = int(max_mb) * 1024 * 1024
        self._snaplen = int(snaplen)
        self._caps: dict[str, _Capture] = {}
        self._lock = threading.Lock()
        self._wd = None

    def start(self, campaign_id, bpf, iface="any") -> dict:
        os.makedirs(self._dir, exist_ok=True)
        cap_id = uuid.uuid4().hex[:12]
        path = os.path.join(self._dir, f"gencall_pcap_{campaign_id}_{cap_id}.pcap")
        cmd = shlex.split(self._command) + [
            "-i", iface, "-w", path, "-s", str(self._snaplen), "-U", bpf,
        ]
        popen_kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if _HAS_SETSID:
            popen_kwargs["preexec_fn"] = os.setsid
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError:
            raise RuntimeError(f"tcpdump not found ({self._command!r}); install it on the worker")
        time.sleep(0.3)
        if proc.poll() is not None:
            raise RuntimeError(
                f"tcpdump exited immediately (code {proc.returncode}); "
                "check it has CAP_NET_RAW / runs as root on this worker")
        cap = _Capture(cap_id, campaign_id, path, proc)
        cap.started_at = time.time()
        with self._lock:
            self._caps[cap_id] = cap
        self._ensure_watchdog()
        return self._info(cap)

    def stop(self, cap_id) -> dict:
        cap = self._get(cap_id)
        if cap.running():
            try:
                if _HAS_SETSID:
                    os.killpg(os.getpgid(cap.proc.pid), signal.SIGTERM)
                else:
                    cap.proc.terminate()
                cap.proc.wait(timeout=5)
            except Exception:
                try:
                    cap.proc.kill()
                except Exception:
                    pass
            cap.stopped_at = time.time()
        return self._info(cap)

    def list(self, campaign_id=None) -> list:
        with self._lock:
            caps = [c for c in self._caps.values()
                    if campaign_id is None or c.campaign_id == campaign_id]
        return [self._info(c) for c in caps]

    def path(self, cap_id) -> str:
        return self._get(cap_id).path

    def delete(self, cap_id) -> None:
        cap = self._get(cap_id)
        if cap.running():
            self.stop(cap_id)
        try:
            if os.path.isfile(cap.path):
                os.remove(cap.path)
        except OSError as e:
            logger.warning("could not delete capture %s: %s", cap.path, e)
        with self._lock:
            self._caps.pop(cap_id, None)

    # ── internals ──
    def _get(self, cap_id) -> "_Capture":
        with self._lock:
            cap = self._caps.get(cap_id)
        if cap is None:
            raise KeyError(cap_id)
        return cap

    def _info(self, cap) -> dict:
        size = os.path.getsize(cap.path) if os.path.isfile(cap.path) else 0
        return {"id": cap.id, "campaign_id": cap.campaign_id,
                "running": cap.running(), "size_bytes": size,
                "started_at": cap.started_at, "stopped_at": cap.stopped_at}

    def _ensure_watchdog(self):
        if (self._max_seconds <= 0 and self._max_bytes <= 0):
            return
        if self._wd is not None and self._wd.is_alive():
            return
        self._wd = threading.Thread(target=self._watch, daemon=True, name="capture-watchdog")
        self._wd.start()

    def _watch(self):
        while True:
            time.sleep(2.0)
            with self._lock:
                caps = list(self._caps.values())
            if not any(c.running() for c in caps):
                return  # idle; a new start() relaunches the watchdog
            now = time.time()
            for c in caps:
                if not c.running():
                    continue
                too_long = self._max_seconds > 0 and c.started_at and (now - c.started_at) > self._max_seconds
                too_big = self._max_bytes > 0 and os.path.isfile(c.path) and os.path.getsize(c.path) > self._max_bytes
                if too_long or too_big:
                    logger.info("auto-stopping capture %s (%s)", c.id,
                                "duration" if too_long else "size")
                    try:
                        self.stop(c.id)
                    except Exception:
                        pass

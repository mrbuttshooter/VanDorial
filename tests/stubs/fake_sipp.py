#!/usr/bin/env python3
"""
Cross-platform fake `sipp` for the GenCall test harness.

It mimics just enough of the real SIPp CLI that `gencall.core.sipp_engine`
builds (see SIPpInstance.build_command) so the process lifecycle — spawn,
stats-file growth, graceful stop, kill-escalation — can be exercised on a
plain Windows/macOS/Linux box with no real SIPp, Docker, or Linux.

What it understands (everything else is ignored, like real SIPp tolerates
unknown trailing flags here):

  -sf <scenario>        scenario file (recorded, not parsed)
  <host:port>           remote target (first bare positional arg)
  -i <ip> / -p <port>   local binding (recorded)
  -t <transport>        transport token (u1/t1/l1)
  -r <rate>             call rate (calls/sec) — drives how fast stats grow
  -m <max>              total calls to create, then exit (0/absent = run until
                        stopped); gives tests a finite, fast lifetime
  -l <limit>            concurrent call limit (caps CurrentCall)
  -d <ms>               per-call hold (recorded; affects CurrentCall drain)
  -inf <csv>            number-pair injection file (recorded)
  -au/-ap <user/pass>   auth (recorded)
  -trace_stat           enable stats CSV (implied by -stf anyway)
  -stf <path>           stats CSV path — we WRITE a SIPp-format file here
  -fd <secs>            stats flush/dump interval (seconds); default 1
  -trace_err/-trace_logs/-bg   accepted, no-ops here
  -cp <port>            optional control port (recorded; accepts no traffic)

Output it produces:

  * A SIPp-format stats CSV at -stf <path>: ';'-separated, a header row then
    one data row per dump tick, columns including TotalCallCreated,
    SuccessfulCall(C), FailedCall(C), CurrentCall, Retransmissions(C). Values
    grow over time at the configured rate. This is exactly what
    SIPpEngine._read_stats() parses (header line + last line).

  * Per-call structured log lines (design §4.2 fields) to a predictable path
    next to the stats file: "<stats-stem>.calllog". One line per call with
    call_id, a/b numbers and the RFC reference-event timestamps so later
    stages have a real per-call log to tail-parse.

Stopping:

  * POSIX: SIGUSR1 (SIPp's graceful-drain convention) or SIGTERM -> clean
    exit 0 after a final stats flush. SIGINT likewise.
  * Windows: SIGTERM (what Popen.terminate() sends) / SIGBREAK / Ctrl-C ->
    clean exit 0.

It always runs a bounded, stoppable lifetime: with -m it exits after creating
that many calls; without -m it runs until signalled (tests stop it explicitly),
with a hard safety cap so a leaked stub can never run forever.
"""

import os
import signal
import sys
import threading
import time

# SIPp's stats CSV is ';'-separated. We emit the subset of columns the engine
# parses plus a couple of the standard leading/period columns so the file looks
# like a real periodic SIPp dump. Header MUST match the data row width.
STATS_COLUMNS = [
    "StartTime",
    "LastResetTime",
    "CurrentTime",
    "ElapsedTime(P)",
    "ElapsedTime(C)",
    "TargetRate",
    "CallRate(P)",
    "CallRate(C)",
    "IncomingCall(P)",
    "IncomingCall(C)",
    "OutgoingCall(P)",
    "OutgoingCall(C)",
    "TotalCallCreated",
    "CurrentCall",
    "SuccessfulCall(P)",
    "SuccessfulCall(C)",
    "FailedCall(P)",
    "FailedCall(C)",
    "Retransmissions(P)",
    "Retransmissions(C)",
]

# Hard safety ceiling: a stub with no -m must never outlive a test run. Tests
# that want a long-lived process stop it explicitly well before this.
_MAX_LIFETIME_S = 120.0

# Signalled-stop flag, flipped from the signal handler. We avoid doing real
# work inside the handler (just set the event) so the main loop can flush and
# exit cleanly.
_stop = threading.Event()


def _handle_stop(signum, _frame):
    _stop.set()


def _install_signal_handlers():
    """Register the broadest set of stop signals the platform offers.

    POSIX: SIGUSR1 is SIPp's graceful-exit convention and is what
    SIPpEngine.stop_instance() sends to the process group; we also take
    SIGTERM/SIGINT. Windows has no SIGUSR1/process groups — Popen.terminate()
    sends SIGTERM, and Ctrl-C/console-close map to SIGINT/SIGBREAK.
    """
    for name in ("SIGUSR1", "SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_stop)
        except (ValueError, OSError):
            # Some signals can't be set off the main thread / on some hosts;
            # skip rather than fail the whole stub.
            pass


def _parse_args(argv):
    """Parse the SIPp-ish CLI into a plain dict. Unknown flags are tolerated."""
    opts = {
        "scenario": "",
        "remote": "",
        "local_ip": "",
        "local_port": "",
        "transport": "",
        "rate": 1.0,
        "max_calls": 0,
        "call_limit": 0,
        "duration_ms": 0,
        "inf": "",
        "auth_user": "",
        "auth_pass": "",
        "stats_file": "",
        "dump_interval": 1.0,
        "control_port": 0,
    }
    # Flags that take exactly one value.
    valued = {
        "-sf": "scenario",
        "-i": "local_ip",
        "-p": "local_port",
        "-t": "transport",
        "-r": "rate",
        "-m": "max_calls",
        "-l": "call_limit",
        "-d": "duration_ms",
        "-inf": "inf",
        "-au": "auth_user",
        "-ap": "auth_pass",
        "-stf": "stats_file",
        "-fd": "dump_interval",
        "-cp": "control_port",
    }
    int_keys = {"max_calls", "call_limit", "duration_ms", "control_port"}
    float_keys = {"rate", "dump_interval"}

    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in valued:
            key = valued[tok]
            val = argv[i + 1] if i + 1 < len(argv) else ""
            if key in int_keys:
                try:
                    opts[key] = int(float(val))
                except (TypeError, ValueError):
                    opts[key] = 0
            elif key in float_keys:
                try:
                    opts[key] = float(val)
                except (TypeError, ValueError):
                    opts[key] = 1.0
            else:
                opts[key] = val
            i += 2
            continue
        if tok.startswith("-"):
            # Valueless flag (-trace_stat, -trace_err, -trace_logs, -bg, ...).
            i += 1
            continue
        # First bare positional is the remote target host:port.
        if not opts["remote"]:
            opts["remote"] = tok
        i += 1
    return opts


def _read_inf_pairs(path):
    """Read A/B number pairs from a SIPp -inf file.

    Real SIPp -inf files start with a 'SEQUENTIAL'/'RANDOM' directive line,
    then ';'-separated value rows. We tolerate either that or a plain CSV; if
    the file is missing/unreadable we synthesize pairs so the stub still
    produces call logs.
    """
    pairs = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        for idx, ln in enumerate(lines):
            if idx == 0 and ln.upper() in ("SEQUENTIAL", "RANDOM", "USERS"):
                continue
            cells = ln.split(";") if ";" in ln else ln.split(",")
            if len(cells) >= 2:
                pairs.append((cells[0], cells[1]))
            elif cells:
                pairs.append((cells[0], cells[0]))
    except OSError:
        pass
    return pairs


def _call_log_path(stats_file):
    """Predictable per-call log path derived from the stats file path."""
    if stats_file:
        stem, _ext = os.path.splitext(stats_file)
        return stem + ".calllog"
    # No stats file given: fall back to cwd so tests can still find it.
    return os.path.join(os.getcwd(), "fake_sipp.calllog")


def _now_ms():
    return int(time.time() * 1000)


def main(argv):
    opts = _parse_args(argv)
    _install_signal_handlers()

    rate = opts["rate"] if opts["rate"] > 0 else 1.0
    # Cap concurrency: 0/absent means "no explicit cap" — use a sane default so
    # CurrentCall stays bounded and realistic.
    limit = opts["call_limit"] if opts["call_limit"] > 0 else 10
    max_calls = opts["max_calls"]  # 0 = until stopped
    dump_interval = opts["dump_interval"] if opts["dump_interval"] > 0 else 1.0
    stats_file = opts["stats_file"]
    call_log = _call_log_path(stats_file)

    inf_pairs = _read_inf_pairs(opts["inf"]) if opts["inf"] else []

    def pair_for(n):
        if inf_pairs:
            return inf_pairs[n % len(inf_pairs)]
        # Synthesize a deterministic A/B pair when no -inf was supplied.
        return (f"1000{n:06d}", f"2000{n:06d}")

    start_wall = time.time()

    # Open both output files up front (truncate). Writing the stats header
    # immediately means an early reader sees a valid (header-only) file.
    stats_fh = open(stats_file, "w", encoding="utf-8", newline="") if stats_file else None
    if stats_fh is not None:
        stats_fh.write(";".join(STATS_COLUMNS) + ";\n")
        stats_fh.flush()
    log_fh = open(call_log, "w", encoding="utf-8", newline="")

    total_created = 0
    successful = 0
    failed = 0
    retrans = 0
    # In-flight calls as a list of (call_seq, end_wall) so CurrentCall drains.
    in_flight = []

    hold_s = (opts["duration_ms"] / 1000.0) if opts["duration_ms"] > 0 else 1.0
    if hold_s <= 0:
        hold_s = 1.0

    def write_stats_row():
        if stats_fh is None:
            return
        now = time.time()
        elapsed = now - start_wall
        current = len(in_flight)
        # Make one in ~25 calls "fail" so FailedCall(C) is non-trivial for tests
        # that look at failure accounting, but keep the vast majority successful.
        row = {
            "StartTime": f"{start_wall:.3f}",
            "LastResetTime": f"{now:.3f}",
            "CurrentTime": f"{now:.3f}",
            "ElapsedTime(P)": f"{dump_interval:.3f}",
            "ElapsedTime(C)": f"{elapsed:.3f}",
            "TargetRate": f"{rate:.3f}",
            "CallRate(P)": f"{rate:.3f}",
            "CallRate(C)": f"{(total_created / elapsed) if elapsed > 0 else 0:.3f}",
            "IncomingCall(P)": "0",
            "IncomingCall(C)": "0",
            "OutgoingCall(P)": str(total_created),
            "OutgoingCall(C)": str(total_created),
            "TotalCallCreated": str(total_created),
            "CurrentCall": str(current),
            "SuccessfulCall(P)": str(successful),
            "SuccessfulCall(C)": str(successful),
            "FailedCall(P)": str(failed),
            "FailedCall(C)": str(failed),
            "Retransmissions(P)": str(retrans),
            "Retransmissions(C)": str(retrans),
        }
        stats_fh.write(";".join(row[c] for c in STATS_COLUMNS) + ";\n")
        stats_fh.flush()

    def write_call_log(seq, a, b, code):
        # Per-call structured line (design §4.2): both UAC reference events.
        # Stored ms-precision; a placebo answer/bye delta so durations are sane.
        t_invite = _now_ms()
        t_200 = t_invite + 120          # ~120 ms to answer
        t_bye = t_200 + int(hold_s * 1000)
        log_fh.write(
            "direction=out "
            f"call_id=fake-{seq:08d}@stub "
            f"a_number={a} b_number={b} "
            f"t_invite={t_invite} t_200ok_received={t_200} t_bye_sent={t_bye} "
            f"final_code={code}\n"
        )
        log_fh.flush()

    # Emit an initial (zeroed) stats row so a reader that catches us very early
    # still sees a header + one data row (engine needs >= 2 lines).
    write_stats_row()

    # Tokens accumulate at `rate` per second; each whole token spawns one call.
    next_dump = start_wall + dump_interval
    accum = 0.0
    last_tick = start_wall
    # Fine tick so signals are observed promptly and CurrentCall drains smoothly.
    tick = min(0.05, dump_interval)

    exit_code = 0
    try:
        while not _stop.is_set():
            now = time.time()

            # Safety ceiling — never outlive a test run.
            if now - start_wall > _MAX_LIFETIME_S:
                break

            # Drain finished calls.
            if in_flight:
                in_flight = [c for c in in_flight if c[1] > now]

            # Spawn new calls per elapsed time * rate, honoring -m and -l.
            accum += (now - last_tick) * rate
            last_tick = now
            while accum >= 1.0:
                accum -= 1.0
                if max_calls > 0 and total_created >= max_calls:
                    break
                if len(in_flight) >= limit:
                    # At concurrency cap — don't create now; try next tick.
                    accum = 0.0
                    break
                total_created += 1
                seq = total_created
                a, b = pair_for(seq)
                # ~1 in 25 fails (503), the rest succeed (200).
                if seq % 25 == 0:
                    failed += 1
                    write_call_log(seq, a, b, 503)
                else:
                    successful += 1
                    in_flight.append((seq, now + hold_s))
                    write_call_log(seq, a, b, 200)

            # Periodic stats dump.
            if now >= next_dump:
                write_stats_row()
                next_dump = now + dump_interval

            # Finite mode: once we've created -m calls and they've all drained,
            # flush a final row and exit 0 (mirrors real SIPp completing).
            if max_calls > 0 and total_created >= max_calls and not in_flight:
                break

            time.sleep(tick)
    finally:
        # Final stats flush so the last row reflects the end state.
        write_stats_row()
        if stats_fh is not None:
            stats_fh.close()
        log_fh.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))

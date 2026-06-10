"""
Per-call record pipeline (design §4.2 / §5).

SIPp's loop scenarios (gencall/scenarios/templates/loop_uac.xml,
loop_uas.xml) emit one structured ``<log>`` line per call per event to SIPp's
``-trace_logs`` file. This module tail-parses those lines into the
``call_records`` table, computing each side's duration in milliseconds at
exactly the RFC 3261 reference events the loop accounting depends on (§1):

    A-side (outbound / UAC):  duration_ms = t_bye_sent      - t_200ok_received
    B-side (inbound  / UAS):  duration_ms = t_bye_received  - t_200ok_sent

Milliseconds are stored RAW; any rounding to whole seconds is display-only.
Failed calls (no answer) store ``final_code`` (404/487/503/...) with
``duration_ms = 0``.

Each scenario writes several lines for one call (invite, answer, bye), each a
flat ``key=value`` token stream. The parser accumulates the events for a given
``(campaign_id, direction, call_id)`` and upserts the merged record, so a record
is filled in incrementally as later events arrive and re-ingesting the same
lines is idempotent (the UNIQUE index in migration 0003 backs the upsert).

A single tail-parser thread polls each tracked log file. Per this codebase's
standard there are **no busy loops**: the poll interval is throttled to >= 1 s
(design §4.2), and the thread sleeps between passes. It is control-plane only —
the calls and media live in native SIPp.
"""

import datetime
import ipaddress
import logging
import os
import threading
import time

logger = logging.getLogger("gencall.call_records")

# Minimum poll interval for the tail loop. The spec mandates >= 1 s for any
# poll/tail loop on the 4 GB box (design §4.2); we floor any smaller request.
MIN_POLL_INTERVAL_S = 1.0


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat()


def ip_in_whitelist(source_ip, whitelist):
    """Return True if ``source_ip`` matches any entry in ``whitelist``.

    Implements the verification-only trust filter (design §4.1). Each whitelist
    entry may be a plain IP (``10.0.0.9``) or a CIDR (``10.0.0.0/24``); a bare IP
    is treated as a /32 (or /128) host. An **empty whitelist means "allow all"**
    (a fresh install isn't broken — the caller notes it instead). A record with
    no ``source_ip`` (e.g. an outbound leg, which has no inbound peer) is not the
    subject of this check and is handled by the caller.

    The host firewall remains the real boundary; this is the in-app visibility
    check so a misconfigured firewall is *seen* rather than silently trusted.
    """
    if not whitelist:
        # Empty whitelist: nothing configured to verify against → allow all.
        return True
    if not source_ip:
        return False
    try:
        addr = ipaddress.ip_address(source_ip)
    except ValueError:
        # An unparseable source can never match a whitelist entry → not trusted.
        return False
    for entry in whitelist:
        entry = (entry or "").strip()
        if not entry:
            continue
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            # Skip a malformed whitelist token rather than abort the whole check.
            continue
        if addr in net:
            return True
    return False


def parse_log_line(line):
    """Parse one SIPp ``<log>`` line into a flat dict of its key=value tokens.

    Lines look like (whitespace-separated, order-tolerant)::

        loop_uac direction=out call_id=abc@h a_number=100 b_number=200 \
            event=answer t_200ok_received=1700000000120 final_code=200

    A leading bare token (``loop_uac`` / ``loop_uas``) is a scenario tag and is
    ignored. Returns ``{}`` for a blank line or one with no ``call_id`` and no
    ``direction`` (i.e. not one of our records — other SIPp log noise is
    skipped). Numeric-looking timestamp/code fields are left as strings here;
    the record builder coerces them so a malformed token never aborts a pass.
    """
    fields = {}
    for tok in line.strip().split():
        if "=" not in tok:
            # Bare scenario tag (loop_uac/loop_uas) or stray token — ignore.
            continue
        key, _, val = tok.partition("=")
        if key:
            fields[key] = val
    # Only treat it as one of ours if it carries an identity we key on.
    if "call_id" not in fields:
        return {}
    return fields


def _as_int(value):
    """Best-effort int coercion; None on anything non-numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Final codes >= 200 that are NOT a 2xx answer mean the call failed before media
# (e.g. 404/486/487/503). Such a record stores final_code with duration 0.
def _is_success_code(code):
    return code is not None and 200 <= code < 300


class _CallAccumulator:
    """Merges the several log lines of one call into a single record dict.

    Keyed by ``(campaign_id, direction, call_id)``. Each parsed log line updates
    the in-progress record; the timestamp fields map to the direction-neutral
    columns of ``call_records`` so the same math serves both sides:

        t_start_ms  = out:t_invite          | in:t_invite_received
        t_answer_ms = out:t_200ok_received  | in:t_200ok_sent
        t_end_ms    = out:t_bye_sent        | in:t_bye_received
    """

    # Per-direction map from a log field name to the neutral record column.
    _TS_FIELDS = {
        "out": {
            "t_invite": "t_start_ms",
            "t_200ok_received": "t_answer_ms",
            "t_bye_sent": "t_end_ms",
        },
        "in": {
            "t_invite_received": "t_start_ms",
            "t_200ok_sent": "t_answer_ms",
            "t_bye_received": "t_end_ms",
        },
    }

    def __init__(self):
        # key -> record dict
        self._records = {}

    @staticmethod
    def key_of(fields):
        """The accumulation key for a parsed line, or None if unusable."""
        call_id = fields.get("call_id")
        if not call_id:
            return None
        direction = fields.get("direction") or "out"
        campaign_id = fields.get("campaign_id")  # may be None
        return (campaign_id, direction, call_id)

    def ingest(self, fields):
        """Fold one parsed log line into its record; returns the record key."""
        key = self.key_of(fields)
        if key is None:
            return None
        campaign_id, direction, call_id = key

        rec = self._records.get(key)
        if rec is None:
            rec = {
                "campaign_id": campaign_id,
                "direction": direction,
                "call_uuid": call_id,
                "a_number": None,
                "b_number": None,
                "source_ip": None,
                "t_start_ms": None,
                "t_answer_ms": None,
                "t_end_ms": None,
                "final_code": None,
            }
            self._records[key] = rec

        # Identity / metadata fields (last non-empty wins).
        if fields.get("a_number"):
            rec["a_number"] = fields["a_number"]
        if fields.get("b_number"):
            rec["b_number"] = fields["b_number"]
        # UAS lines name the parties from_number/to_number; map onto a/b so a
        # single record shape serves both directions (matcher keys on b_number).
        if fields.get("from_number"):
            rec["a_number"] = fields["from_number"]
        if fields.get("to_number"):
            rec["b_number"] = fields["to_number"]
        if fields.get("source_ip"):
            rec["source_ip"] = fields["source_ip"]

        # Timestamp fields, mapped by direction to neutral columns.
        ts_map = self._TS_FIELDS.get(direction, self._TS_FIELDS["out"])
        for log_field, column in ts_map.items():
            if log_field in fields:
                val = _as_int(fields[log_field])
                if val is not None:
                    rec[column] = val

        # Final SIP response code (200 answer, or a failure code).
        if "final_code" in fields:
            code = _as_int(fields["final_code"])
            if code is not None:
                rec["final_code"] = code

        return key

    def finalize(self, key):
        """Compute duration_ms for a record and return a row ready to persist.

        A-side / B-side duration is ``t_end_ms - t_answer_ms`` (the same neutral
        math for both directions). A call that never answered (no t_answer_ms)
        is a failure: duration 0, final_code preserved (defaults to 0 if the
        scenario logged no code). Durations are clamped to >= 0 so a clock
        anomaly can never store a negative minute.
        """
        rec = self._records.get(key)
        if rec is None:
            return None

        answered = rec["t_answer_ms"] is not None
        ended = rec["t_end_ms"] is not None

        final_code = rec["final_code"]
        if final_code is None:
            # No explicit code logged: a fully-timed call is a 200 answer;
            # anything else is an unknown failure recorded as 0 so the column is
            # never NULL.
            final_code = 200 if answered else 0

        # A non-2xx final code is a failure: duration 0 regardless of any
        # timestamps the scenario happened to log (design §4.2 — "failed calls
        # store final_code ... with duration 0"). Only a successful, fully-timed
        # call carries a real duration.
        if _is_success_code(final_code) and answered and ended:
            duration = rec["t_end_ms"] - rec["t_answer_ms"]
            if duration < 0:
                duration = 0
        else:
            duration = 0

        row = dict(rec)
        row["duration_ms"] = duration
        row["final_code"] = final_code
        return row

    def pop_complete(self):
        """Yield (key, row) for every record that has reached a terminal event.

        A record is terminal once its end timestamp is set (BYE logged) or it
        carries a non-success final_code (a failure that never answered). Such
        records are removed from the accumulator after yielding so memory stays
        bounded over a long campaign; partial records (still awaiting BYE) are
        left in place for the next pass.
        """
        done_keys = []
        for key, rec in self._records.items():
            terminal = rec["t_end_ms"] is not None
            code = rec["final_code"]
            failed = code is not None and not _is_success_code(code)
            if terminal or failed:
                done_keys.append(key)
        for key in done_keys:
            row = self.finalize(key)
            del self._records[key]
            yield key, row


class CallRecordParser:
    """Throttled tail-parser: SIPp log files -> ``call_records`` rows.

    Tracks a set of log files (one per running SIPp instance). Each pass reads
    only the bytes appended since the last pass (a stored byte offset per file),
    folds new lines into per-call accumulators, and upserts every record that
    has reached a terminal event into the DB.

    Designed for the control plane: the poll loop sleeps >= ``MIN_POLL_INTERVAL_S``
    between passes (no busy loop), and per-file offsets mean a growing log is
    never re-read from the top.
    """

    def __init__(self, db=None, poll_interval=MIN_POLL_INTERVAL_S,
                 trust_whitelist=None, drop_untrusted=False):
        # ``db`` is a gencall.db.models.Database (or None for parse-only tests).
        self.db = db
        # Floor the interval at the mandated minimum — never busy-poll (§4.2).
        self.poll_interval = max(float(poll_interval), MIN_POLL_INTERVAL_S)
        # Verification-only trust filter (design §4.1). Inbound records whose
        # source_ip is not in this list are flagged untrusted (and dropped if
        # ``drop_untrusted``) so a misconfigured firewall is visible rather than
        # silently trusted. The host firewall is the real boundary. An empty list
        # means "allow all + note" so a fresh install isn't broken.
        self.trust_whitelist = list(trust_whitelist or [])
        self.drop_untrusted = bool(drop_untrusted)
        # path -> {"offset": int, "campaign_id": str|None, "acc": _CallAccumulator}
        self._files = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ── file registration ───────────────────────────────────────────────────

    def add_log_file(self, path, campaign_id=None):
        """Start tracking a SIPp log file (idempotent).

        ``campaign_id`` tags every record parsed from this file when the log
        lines do not carry one themselves (the scenarios omit it; the engine
        knows which campaign owns each instance).
        """
        with self._lock:
            if path not in self._files:
                self._files[path] = {
                    "offset": 0,
                    "campaign_id": campaign_id,
                    "acc": _CallAccumulator(),
                }

    def remove_log_file(self, path):
        """Stop tracking a log file (e.g. its instance was removed)."""
        with self._lock:
            self._files.pop(path, None)

    # ── parsing ──────────────────────────────────────────────────────────────

    def _read_new_lines(self, path, state):
        """Read bytes appended since the stored offset; return list[str] lines.

        The offset is a TRUE BYTE offset and the file is read in BINARY mode:
        text-mode ``seek()`` on a multibyte/CRLF log mixes byte and character
        positions (Python text streams only allow seeking to opaque cookies),
        so a byte offset fed to a text-mode seek corrupts the read on any
        non-ASCII or CRLF line. We therefore seek/read raw bytes, split on the
        last newline at the byte level, advance the offset by the exact byte
        count consumed, and only then decode the consumed bytes to str. Only
        complete lines are consumed; a trailing partial line is left for the
        next pass.
        """
        if not os.path.exists(path):
            return []
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        if size < state["offset"]:
            # File shrank/rotated — restart from the top.
            state["offset"] = 0
        if size == state["offset"]:
            return []
        try:
            with open(path, "rb") as fh:
                fh.seek(state["offset"])
                chunk = fh.read(size - state["offset"])
        except OSError as e:
            logger.debug("Could not read log file %s: %s", path, e)
            return []

        # Find the last newline at the BYTE level so the offset stays a byte
        # offset (b"\n" is 0x0A and never appears inside a UTF-8 multibyte
        # sequence, so splitting on it never bisects a character).
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            # No complete line yet; wait for more bytes.
            return []
        consumed = chunk[: last_nl + 1]
        # Advance by the exact number of BYTES consumed (not re-encoded chars).
        state["offset"] += len(consumed)
        text_consumed = consumed.decode("utf-8", errors="replace")
        return [ln for ln in text_consumed.splitlines() if ln.strip()]

    def poll_once(self):
        """Run one parse pass over every tracked file; persist terminal records.

        Returns the list of finalized row dicts persisted this pass (handy for
        tests). Safe to call directly without the background thread.
        """
        finalized = []
        with self._lock:
            items = list(self._files.items())
        for path, state in items:
            acc = state["acc"]
            default_campaign = state["campaign_id"]
            for line in self._read_new_lines(path, state):
                fields = parse_log_line(line)
                if not fields:
                    continue
                # Apply the file's owning campaign when the line omits one.
                if default_campaign is not None and not fields.get("campaign_id"):
                    fields["campaign_id"] = default_campaign
                acc.ingest(fields)
            for _key, row in acc.pop_complete():
                row = self._apply_trust_filter(row)
                if row is None:
                    # Dropped: outside the whitelist and drop_untrusted is on.
                    continue
                self._persist(row)
                finalized.append(row)
        return finalized

    def _apply_trust_filter(self, row):
        """Verification-only inbound trust check (design §4.1).

        Tags every record with a ``trusted`` flag. Only *inbound* records carry a
        ``source_ip`` (the network peer); an inbound record whose source is not in
        ``trust_whitelist`` is flagged untrusted and a warning is logged so a
        misconfigured firewall is visible rather than silently trusted. When
        ``drop_untrusted`` is set such a record is dropped (returns None) instead
        of persisted. Outbound records (no inbound peer) are always trusted. An
        empty whitelist allows all (``ip_in_whitelist`` returns True) so a fresh
        install isn't broken — but we still note the first such inbound call.
        """
        direction = row.get("direction")
        if direction != "in":
            row["trusted"] = True
            return row

        source_ip = row.get("source_ip")
        if ip_in_whitelist(source_ip, self.trust_whitelist):
            row["trusted"] = True
            if not self.trust_whitelist:
                # Allow-all-with-note: empty whitelist on a fresh install. Log
                # once-ish at debug so the operator knows nothing is enforced.
                logger.debug(
                    "Inbound call_record %s from %s accepted: trust_whitelist "
                    "empty (allow-all). Set [trust] whitelist to enforce.",
                    row.get("call_uuid"), source_ip,
                )
            return row

        # Outside the whitelist: flag (and optionally drop) so it is visible.
        row["trusted"] = False
        logger.warning(
            "Inbound call_record %s from non-whitelisted source %s "
            "(firewall misconfigured?); %s.",
            row.get("call_uuid"), source_ip,
            "dropping" if self.drop_untrusted else "flagged untrusted",
        )
        if self.drop_untrusted:
            return None
        return row

    # ── persistence (raw SQL, idempotent upsert) ─────────────────────────────

    def _persist(self, row):
        """Upsert one finalized record into ``call_records`` (no-op if no DB).

        The UNIQUE index on (campaign_id, direction, call_uuid) makes this
        idempotent: re-ingesting the same call updates the existing row (so a
        record filled in across passes converges) rather than duplicating it.
        """
        if self.db is None:
            return
        try:
            from sqlalchemy import text

            params = dict(row)
            params["created_at"] = _now_iso()
            with self.db.engine.begin() as conn:
                existing = conn.execute(
                    text(
                        "SELECT id FROM call_records "
                        "WHERE call_uuid = :call_uuid AND direction = :direction "
                        # NULL-safe equality: 'IS :param' is SQLite-only and is a
                        # syntax error on PostgreSQL. 'IS NOT DISTINCT FROM' is
                        # the standard NULL-safe comparison and works on both
                        # (matches NULL=NULL for one-shot tests with no campaign).
                        "AND campaign_id IS NOT DISTINCT FROM :campaign_id"
                    ),
                    {
                        "call_uuid": params["call_uuid"],
                        "direction": params["direction"],
                        "campaign_id": params["campaign_id"],
                    },
                ).fetchone()
                if existing:
                    conn.execute(
                        text(
                            "UPDATE call_records SET "
                            "a_number = :a_number, b_number = :b_number, "
                            "source_ip = :source_ip, t_start_ms = :t_start_ms, "
                            "t_answer_ms = :t_answer_ms, t_end_ms = :t_end_ms, "
                            "duration_ms = :duration_ms, final_code = :final_code "
                            "WHERE id = :id"
                        ),
                        {**params, "id": existing[0]},
                    )
                else:
                    conn.execute(
                        text(
                            "INSERT INTO call_records "
                            "(campaign_id, direction, call_uuid, a_number, b_number, "
                            " source_ip, t_start_ms, t_answer_ms, t_end_ms, "
                            " duration_ms, final_code, created_at) "
                            "VALUES (:campaign_id, :direction, :call_uuid, :a_number, "
                            " :b_number, :source_ip, :t_start_ms, :t_answer_ms, "
                            " :t_end_ms, :duration_ms, :final_code, :created_at)"
                        ),
                        params,
                    )
        except Exception as e:
            logger.warning("Could not persist call_record %s: %s",
                           row.get("call_uuid"), e)

    # ── background loop (throttled, >= 1 s — no busy poll) ───────────────────

    def start(self):
        """Start the background tail-parser thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="call-record-parser"
        )
        self._thread.start()

    def stop(self, timeout=5.0):
        """Signal the loop to exit and join the thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self):
        # Event-driven sleep: wakes early only on stop(), otherwise idles for the
        # full (>= 1 s) interval so the control plane stays near-zero CPU.
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Call-record parse pass failed: %s", e)
            self._stop.wait(self.poll_interval)

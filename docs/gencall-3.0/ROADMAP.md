# GenCall 3.0 — Architecture Roadmap

> **North star:** GenCall 3.0 = a hardened, **engine-pluggable, observable,
> minutes-for-minutes** loop platform that scales from one air-gapped box to a
> 30-node fleet **without ever touching the sacred SIPp call path**
> (`loop_*.xml` / INVITE / codec + media-IP flags / E.164 number generation).

This roadmap was produced by the Wave-1 architecture pass of the
`improve/gencall-3.0-loop` branch and is grounded in the current code
(`gencall/`, `frontend/`). It is a direction, not a committed plan — each theme
lands incrementally, behind the existing test gate, with the call path untouched.

---

## The six themes

### 1. A real `TrafficEngine` abstraction — SIPp today, pure-Python tomorrow  ·  transformational · XL · ⚠ call-path-adjacent
**Problem today:** `LoopEngine` is welded to SIPp — it imports `SIPpInstance`/
`SIPpMode`/`SIPpTransport` directly and every campaign concept (rate, hold, RTP,
media-port, `-inf` CSV) is a SIPp flag assembled in `sipp_engine.build_command`.
A second engine (the colleague's pure-Python no-SIPp generator) can't slot in.

**Proposal:** Introduce a `TrafficEngine` Protocol
(`start_originator/start_answerer/stop/live_stats/emit_record`) and an
engine-neutral `CampaignSpec`. Refactor today's SIPp code into a `SippEngine`
adapter — **the `loop_*.xml`, INVITE, media/codec flags and E.164 format move
_behind_ the adapter UNCHANGED (moved, never edited).** `LoopEngine` then talks
only to the Protocol.

**Risk / guardrail:** Any seam near SIPp argv assembly could silently change the
wire message and break live routes (the cause-47/127 + 404 history proves how
fragile media-IP/codec handling is). **Requires human sign-off; gated behind an
on-box smoke-loopback + working-vs-ours pcap diff; the SIPp adapter must emit a
byte-identical command on day one.**

### 2. Streaming CDR + metrics pipeline (replace file-tail + REST polling)  ·  high · L · ✅ call-path-safe
**Problem today:** Telemetry is scraped off disk — `SIPpEngine._read_stats`
re-reads a CSV each interval, `CallRecordParser` tail-reads per-call `<log>`
files by byte offset, and the controller REST-polls every worker at 1 Hz.

**Proposal:** Define a stable internal **CDR event contract** emitted by the
engine adapter (the SIPp adapter keeps producing it by tailing `<log>`, so the
call path is untouched) feeding one ingestion path into (a) a bounded,
downsampled time-series for live dashboards and (b) an append-only CDR store with
explicit roll-off to an operator-chosen sink.

**Risk / guardrail:** Loop accounting is the product's dispute-resolution
evidence — the CDR contract must be a **pure re-plumbing** of what the tail-parser
already computes (no change to minutes-out/minutes-in). Push telemetry must
degrade to today's REST poll on WS-drop, never blank the dashboard. Longer
retention re-raises the disk-fill risk that drove the 1-day default → roll-off
must be off-box or hard size-capped.

### 3. Config-as-code: declarative campaigns, fleets, boxes  ·  high · L · ✅ call-path-safe
**Problem today:** Config is a stdlib `configparser` singleton with ~50 ad-hoc
typed properties and only soft-warning validation; campaigns are imperative API
calls with ~25 kwargs; fleet launches are fire-and-forget POSTs.

**Proposal:** A schema-validated campaign/fleet **manifest** the controller can
apply/diff/reconcile toward — desired-state fleet ops instead of transient POSTs.
Give `Config` a typed schema with real validation surfaced in the console.
**Stdlib-only on the worker stays absolute** (no crudini/pip at runtime; parsing
stays `configparser`/`json`).

**Risk / guardrail:** Reconcile loops must be **diff-preview-then-apply, never
silent** — a bug could thrash real dialers or relaunch a campaign the operator
stopped. The trust whitelist stays **OFF-by-default** in any generated manifest
for the operator, ON only for a client sale.

### 4. Single-box concurrency: break the one-process ceiling safely  ·  medium · L · ✅ call-path-safe
**Problem today:** The worker is hard-capped to one process (`main.py` refuses
`--workers > 1`) because the engine instance map, `ProcessRegistry`, running-loop
set and the "one loop per IP" guard are in-process module globals.

**Proposal:** Make reliability state (managed PIDs, running campaigns,
IP-exclusivity) authoritatively **DB-backed** so orphan reconciliation, shutdown
`stop_all`, and the one-loop-per-IP guard hold across multiple control-plane
workers. Keep it opt-in; default stays single-process.

**Risk / guardrail:** Moving PID/campaign ownership to the DB introduces
cross-process start/stop/reconcile races the single-process locks make impossible
today (design §8's UAS-fighting-for-:5060 race). Needs careful DB-level locking;
only worth it on boxes big enough to matter.

### 5. Observability + operator UX for the _actual_ failure modes  ·  medium · M · ✅ call-path-safe
**Problem today:** The system already learns routable prefixes
(`pool_optimizer`) and shapes diurnal traffic (`traffic_profile` + shaper), but
the operator view is mostly live tiles. The real workflow — "debug OUR side, not
the switch; prove it with a working-vs-ours pcap" — has no first-class UI.

**Proposal:** A "why" layer on data already captured — adaptive-pool keep/drop
decisions with ASR evidence, shaper rate-step history, per-code failure trends
(already in `loop_matcher`), plus a guided **capture-to-diff** flow pairing an
ours-vs-working pcap for the cause-47/media class.

**Risk / guardrail:** Low blast radius (additive/frontend). The UI must show
**evidence, not a diagnosis** — never an over-confident "switch side" verdict that
contradicts the hard rule that loop failures are usually ours.

### 6. Deterministic offline packaging + an enforced call-path test gate  ·  medium · M · ✅ call-path-safe
**Problem today:** Offline install ships vendored debs + a wheelhouse +
`virtualenv.pyz`, ABI-locked to Ubuntu 22.04 / Python 3.10. The three-way RTP
port window (`gencall.cfg` / `docker-compose.v2.yml` / firewall) is maintained by
hand and drifts.

**Proposal:** Reproducible, self-describing packaging — pin/lock the wheelhouse +
deb set to a manifest, **generate the RTP-window triple from one source** so
cfg/compose/firewall can't drift, keep the version/ABI preflight. Most
importantly, formalize a **call-path test gate**: a required on-box job
(smoke-loopback + a golden working-vs-ours INVITE/pcap comparison) that must pass
before any call-path-adjacent change merges.

**Risk / guardrail:** The golden-pcap gate needs a real Linux/SIPp box (can't run
in the Windows dev sandbox) → a CI-infra investment. The diff must target the
load-bearing INVITE fields (R-URI, From/To, SDP codec/media-IP), not whitespace.

---

## Quick wins (low-risk seams that make the big themes incremental)

1. **Single-source the RTP port window** → emit it into `gencall.cfg`,
   `docker-compose.v2.yml` and the firewall rules from one place, killing the
   hand-maintained three-way "must match" drift.
2. **Extract an internal CDR event dataclass** that `CallRecordParser` emits (the
   same fields it already computes) — a zero-behavior-change seam that makes both
   the streaming pipeline and the pluggable engine incremental instead of a
   rewrite.
3. **Persist adaptive-pool keep/drop decisions + shaper rate-steps** (already
   logged) into a small events table so the console can show the "why".
4. **Reconcile the launch/stop path onto `/api/loops` consistently** and document
   the `/api/tests` vs `/api/loops` split so fleet dispatch and telemetry name
   the same thing.
5. **Add typed schema validation to `Config`** that fails loudly on genuinely
   invalid combos (e.g. `max_answered_calls < max_channels`, or
   `min_rtp_port >= max_rtp_port`) instead of only warning.

---

## Sequencing

**Phase A (now → foundations, all call-path-safe):** quick wins 1, 2, 5; the CDR
event dataclass seam; typed `Config` validation. These de-risk everything else.

**Phase B (observability + concurrency):** themes 5, then 2, then 4 — all
call-path-safe, high operator value.

**Phase C (config-as-code):** theme 3 with diff-preview-then-apply.

**Phase D (the big one):** theme 1, the `TrafficEngine` abstraction — **only**
once theme 6's on-box call-path gate exists to prove byte-identical SIPp output.

> The ordering is deliberate: every call-path-safe win ships first and the gate
> that protects the sacred path is built _before_ the refactor that moves it.

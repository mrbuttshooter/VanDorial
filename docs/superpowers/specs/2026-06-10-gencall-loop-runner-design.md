# GenCall v2 — Loop Runner: Design Spec

**Date:** 2026-06-10
**Branch:** `v2/loop-runner`
**Status:** Approved by user (this session); supersedes nothing — first spec for v2.

## 1. Context & motivation

VanDorial currently pays a yearly license for "sigma" (NetAxis), a closed, Cython-compiled
SIP load-test product with documented CPU pathologies (idle busy-poll, RTP socket hygiene —
see `sigma-re/ANALYSIS/REVERSE_ENGINEERING.md` and the runtime hotfix in `sigma-re/patch/`).
The in-house replacement, GenCall (this repo), has a clean wired core (SIPp engine, worker
API, fleet controller, auth gateway) but a production audit (2026-06-10, this session) found:

- ~15 of ~25 `gencall/core` modules are **dead code** (imported nowhere).
- The live rate-change endpoint is a **placebo** (sets a field, never tells SIPp).
- `/ws/cdr`, `/ws/sip`, `/ws/alerts` topics are never fed; `avg_response_time_ms` is never parsed (always 0).
- **No shutdown/orphan handling**: killing GenCall leaves SIPp children dialing unmanaged.
- No restart recovery (running tests live only in an in-memory dict).
- SIPp is **not installed** in the worker image — the product cannot place a call.
- `--workers > 1` silently breaks (module-global state).
- 32 unit tests; no process-lifecycle/integration coverage.

### The business

VanDorial's commercial runs **minutes-for-minutes loop deals**:

```
VanDorial ──(originate)──► Customer ──► Customer 2 ──► MADA switch ──► VanDorial (answers)
```

VanDorial is **both ends of the loop**: it originates the A-leg and answers the returning
leg (arriving from its own switch, MADA). All parties in the chain are counterparties to a
consensual traffic-exchange deal.

Per RFC 3261 timer mechanics (user's reference deck `SIP_Duration_Discrepancy_v4.pptx`),
the two sides of any SIP call record different durations:

- **A-side** (caller): timer starts when **200 OK is received** (ACK sent), stops when
  **BYE is sent** → structurally shorter.
- **B-side** (callee): timer starts when **200 OK is sent**, stops when **BYE is received**
  → structurally longer (carries 200 OK + BYE propagation, typically 0.25–0.7 s on a
  4-hop international route; real capture showed 0.343 s), plus up to 1 s from CDR rounding.

In the loop, VanDorial is A-side outbound (shorter minutes) and B-side inbound (longer
minutes); the per-call delta is the margin. GenCall v2 must measure **at exactly those
reference events**, in milliseconds, so its numbers match the dispute-resolution
methodology already used commercially.

### Target environment

- One Ubuntu box: **4 vCPU, 4 GB RAM, 40 GB disk** (Docker / Compose v2). Efficiency is a
  hard requirement — Python must stay control-plane only (validated this session: all
  GenCall loops are throttled/event-driven; calls/media live in native SIPp).
- Multi-box fleet via the existing controller is to be **kept working** (user will
  eventually run both single-box and fleet).

## 2. Goals

1. **Loop Campaign** — the one primary object: start/stop sustained originate traffic with
   number-pair CSV, rate, concurrency, per-call duration; runs until stopped or until a
   target (calls or minutes) is reached.
2. **Answering side** — permanently answer calls arriving from MADA (whitelisted IPs only),
   with two-way RTP (echo), hard concurrency cap, and max-duration safety timer.
3. **Loop accounting** — per-call records timestamped at the RFC reference events
   (ms precision); match returning calls to originated calls by number pair; report
   minutes-out vs minutes-in, loop completion %, unreturned pairs, per-call delta
   distribution; CSV export.
4. **Call-health truth** — live ASR, ACD, in-progress count, and failures broken down by
   SIP response code (e.g. visible 487/503 spikes).
5. **Production reliability** — SIPp in the image; zero orphaned dialers (shutdown +
   startup reconciliation); campaign state in PostgreSQL surviving restarts; honest
   controls (real or removed); single-worker guard.
6. **Lean codebase** — delete dead modules; what exists is what runs.
7. **Tests** — integration tests of the process lifecycle against a stub `sipp`.

## 3. Non-goals (v1 of this spec)

- Billing-grade rating/invoicing or carrier CDR reconciliation imports.
- SIP REGISTER support (MADA routes to GenCall's IP; add later if a partner needs it).
- Codecs beyond G.711 (alaw/ulaw); no transcoding.
- The answering side originating anything (it only answers).
- Scheduler/cron campaigns (the existing unwired `core/scheduler.py` stays out of scope;
  it is deleted with the dead code and can return in a later spec if wanted).
- Multi-tenant auth/roles beyond the existing API-key gateway.

## 4. Architecture

Unchanged shape, hardened: **worker** (FastAPI + SIPp engine, port 8080, SIP 5060) +
**controller** (fleet console, port 8090) + **PostgreSQL**. Browser talks to the
controller; controller talks to workers with X-API-Keys. New in v2: the worker gains a
**LoopEngine** built on two SIPp roles and a **CallRecord pipeline**.

```
                      ┌────────────────────── worker ──────────────────────┐
 console ── controller│  FastAPI API ── LoopEngine ──┬── SIPp UAC (outbound legs)
   :8090      :8090   │                              └── SIPp UAS (answer legs, rtp_echo)
                      │  CallRecord pipeline: per-call scenario logs ──► parser ──► Postgres
                      │  LoopMatcher: join out/in records on number pair ──► loop stats
                      └────────────────────────────────────────────────────┘
```

### 4.1 LoopEngine (new, `gencall/core/loop_engine.py`)

Owns one **persistent UAS** SIPp process (answer side) and N **UAC** SIPp processes (one
per running Loop Campaign). Responsibilities: build scenario XML from templates with the
campaign's parameters, start/stop processes via the existing `SIPpEngine` process-control
code (kept), enforce caps, and register every spawned PID in the DB (see 4.5).

**Outbound (UAC)**: custom scenario XML — INVITE with A/B pair injected from the campaign
CSV (`-inf`), play G.711 audio (pcap play), hold for `duration` (fixed or uniform random
range), then BYE. Rate via `-r`, concurrency via `-l`, total via `-m` (0 = until stopped).

**Answer (UAS)**: one long-lived SIPp UAS on the SIP port with `-rtp_echo` (two-way media
by echoing). Custom UAS scenario: answer, log identity/timestamps, wait for BYE; a
scenario-level max-duration guard (timeout → BYE from our side) bounds stuck calls.

**Trust filter**: inbound SIP is accepted only from configured source IPs (MADA, plus any
extras). **Primary enforcement is the host firewall** (deploy docs ship an `nftables`/`ufw`
rule set restricting UDP/5060 + the RTP range to the whitelist — this is the real security
boundary). The app layer is verification-only: the parser tags each inbound `call_record`
with `source_ip` and drops/flags any from outside the whitelist, so a misconfigured firewall
is visible rather than silently trusted. Default-deny at the firewall.

**Caps (config, defaults for the 4 GB box)**: max concurrent loops 50 (= 100 channels),
max answered calls 120, answered-call max duration 7200 s, RTP via host networking.

### 4.2 Per-call records (`gencall/core/call_records.py`)

SIPp scenarios use `<log>` actions writing one structured line per call per event to a
per-process log file (RTT-safe, no message tracing):

- UAC leg: `call_id, a_number, b_number, t_invite, t_200ok_received, t_bye_sent,
  final_code` — A-side duration = `t_bye_sent − t_200ok_received` (ms).
- UAS leg: `call_id, from_number, to_number, source_ip, t_invite_received, t_200ok_sent,
  t_bye_received` — B-side duration = `t_bye_received − t_200ok_sent` (ms).

A tail-parser thread (throttled, ≥1 s interval — no busy loops, per this codebase's
standard) ingests new lines into Postgres `call_records`. Failed calls record
`final_code` (404/487/503/…) with zero duration. Milliseconds are stored raw; any
rounding is display-only.

### 4.3 LoopMatcher (`gencall/core/loop_matcher.py`)

Periodic job (DB query, every ~10 s while a campaign runs): join outbound and inbound
`call_records` on the number pair within a configurable window (default 1 h). Produces
per-campaign aggregates persisted to `loop_stats`:

- calls out / answered out / minutes out (A-side ms summed),
- calls in (matched) / minutes in (B-side ms summed),
- **loop completion %**, list of unmatched pairs,
- per-call delta (in_ms − out_ms): avg / p50 / p95 and a small histogram,
- failures by SIP code (outbound and inbound separately).

Matching is heuristic by design (the chain may rewrite numbers); the match key
(`b_number` exact, or suffix-N digits) is configurable per campaign. Unmatched inbound
calls are still counted in totals so minutes-in is never understated.

### 4.4 API & console

Worker API (existing FastAPI app, new routers):

- `POST /api/loops` start campaign; `POST /api/loops/{id}/stop`; `GET /api/loops`,
  `GET /api/loops/{id}` (live stats incl. loop_stats); `GET /api/loops/{id}/records.csv`
  (export); `GET /api/answer/status` (UAS health, current answered calls).
- Existing `/api/tests/*` (one-shot tests) remain — useful for route checks.
- The rate endpoint: attempt real dynamic rate via SIPp's control socket
  (`-cp` UDP control port, `c` rate commands). If reliable → wire it for loop campaigns;
  if not provable in testing → **delete the endpoint and UI control** (no placebo).

Controller: fleet launch gains a "loop campaign" payload type (same fan-out path as
`/api/fleet/launch`); console gets a **Loops** page (start form + live campaign cards:
ASR/ACD, out/in minutes, completion %, failure-code panel) replacing the dead Scheduler
page. WebSocket: keep `/ws/stats`; feed a new `loops` topic from LoopMatcher output;
**delete** the never-fed `cdr`/`sip`/`alerts` topics.

### 4.5 Reliability mechanics

- **PID registry**: every spawned SIPp PID + role + campaign id is written to a
  `managed_processes` DB table (and a fallback JSON file if DB is down) at spawn time.
- **Shutdown**: FastAPI lifespan shutdown → `stop_all()` (graceful SIGUSR1, then SIGKILL
  to the process group) — already implemented in `SIPpEngine`, now actually called.
- **Startup reconciliation**: read `managed_processes`, kill any PID that still exists and
  matches `sipp` cmdline (guard against PID reuse), mark interrupted campaigns
  `interrupted` in DB. The console shows them so a restart never hides state.
- **Single-process guard**: refuse `--workers > 1` with a clear error.
- **DB migrations**: plain ordered SQL migration files applied at startup (no Alembic
  dependency; the schema is small).
- Replace deprecated `datetime.utcnow()` with timezone-aware calls in touched files.

### 4.6 Deletions (dead code — full list)

`gencall/core/`: `alerts.py`, `capacity_finder.py`, `geo_traffic.py`, `network_sim.py`,
`topology_mapper.py`, `plugin_system.py`, `replay_engine.py`, `call_recorder.py`,
`report_generator.py`, `srtp.py`, `sip_debug.py`, `pcap_analyzer.py`, `cdr.py`,
`live_dashboard_v2.py`, `banner.py`, `scheduler.py`, and `rtp.py` (Python RTP — unused;
SIPp owns media). `gencall/scenarios/scripts/` (the 8 Python scenario scripts — unwired).
Frontend: Scheduler page (replaced by Loops). All `__pycache__` and the stale
`web/__pycache__` artifacts. Tests covering deleted helpers are removed with them.
Anything later found imported stays — verify with grep before each deletion.

## 5. Data model (Postgres)

```sql
loop_campaigns(id, name, status[running|stopped|interrupted|completed], node_id NULL,
               dest_host, dest_port, transport, csv_path, rate, max_concurrent,
               duration_mode[fixed|range], duration_s, duration_max_s,
               match_key[exact|suffix6|suffix8|...], target_calls, target_minutes,
               created_at, started_at, stopped_at)
call_records(id, campaign_id NULL, direction[out|in], call_uuid, a_number, b_number,
             source_ip NULL, t_start_ms, t_answer_ms, t_end_ms, duration_ms,
             final_code, matched_record_id NULL, created_at)
loop_stats(campaign_id, ts, calls_out, answered_out, minutes_out_ms, calls_in_matched,
           minutes_in_ms, completion_pct, delta_avg_ms, delta_p50_ms, delta_p95_ms,
           failures_json)
managed_processes(pid, role[uac|uas], campaign_id NULL, cmdline_hash, spawned_at)
```

`call_records` is the growth table: at 50 concurrent loops / ~3-min calls ≈ 24k
records/day/direction. A retention job (config, default 30 days) prunes it — **interval-
gated**, never per-iteration (we will not rebuild sigma's DELETE storm).

## 6. Testing strategy

1. **Unit**: scenario XML generation, log-line parsing, matcher join logic (incl. suffix
   matching, window edges, unmatched counting), duration math against the PPT's worked
   example (60 s call, 100 ms propagation → A 60.000 s / B 60.205 s).
2. **Integration (stub sipp)**: a fake `sipp` executable (Python script) that emits
   realistic stats CSV + per-call logs and respects signals — tests cover: spawn,
   graceful stop, kill-escalation, crash-orphan reconciliation on startup, restart
   recovery of campaign status, caps enforcement.
3. **End-to-end (real SIPp, CI + on-box)**: worker UAC → worker UAS over loopback —
   real INVITE/200/ACK/BYE with rtp_echo; assert call_records appear with sane A/B-side
   durations and the matcher closes the loop at 100%.
4. **Box validation checklist** (manual, documented): deploy to the 4 GB host, run a
   50-loop campaign 1 h, verify CPU envelope, no fd growth, completion % vs MADA's own
   counters.

## 7. Build approach & sequencing

Work happens on `v2/loop-runner` in reviewable stages, each leaving the repo green:

1. SIPp in the image + end-to-end smoke (UAC→UAS loopback) — *unblocks everything*.
2. Reliability base: PID registry, shutdown stop_all, startup reconciliation, workers guard.
3. Dead-code deletion sweep (greps first; tests still green after).
4. Per-call records: scenario templates + log parser + DB migrations.
5. LoopEngine + campaign API (start/stop/status/CSV export).
6. LoopMatcher + loop_stats + WS `loops` topic.
7. Console Loops page; remove Scheduler page; failure-code panel.
8. Rate control: prove SIPp `-cp` or delete the endpoint.
9. Fleet payload for loop campaigns; controller aggregation of loop_stats.
10. Hardening pass: retention job, 4 GB defaults, deploy docs (host networking, firewall).

## 8. Risks & mitigations

- **SIPp UAS as a single long-lived process** — if it dies, answering stops. Mitigation:
  engine monitors and restarts it (throttled backoff), alert in console health.
- **Number rewriting in the chain breaks matching** — mitigated by configurable suffix
  matching + counting unmatched inbound in totals; completion % is explicitly heuristic.
- **SIPp control-port rate change may be flaky** — explicit prove-or-delete decision gate
  (stage 8); no placebo survives.
- **4 GB box limits** — conservative defaults, measured before raising; host networking
  avoids docker-proxy memory blowup (already learned on this fleet).
- **Log-tail parsing lag** — stats may trail live calls by a few seconds; acceptable for
  this use; documented in UI ("updated Ns ago").

## 9. Success criteria

- A loop campaign started from the console sustains its configured rate for hours on the
  4 GB box with GenCall (Python) under ~5 % CPU and zero orphan processes after any stop,
  crash, or reboot.
- The Loops page answers, at a glance: are calls connecting (ASR/ACD), why are they
  failing (per-SIP-code), is the loop closing (completion %), and what are minutes out vs
  minutes in (with per-call delta distribution) — exportable to CSV.
- Recorded durations follow the RFC reference events to the millisecond, reproducing the
  PPT's worked example in tests.
- `gencall/` contains no module that is not imported by the running system.

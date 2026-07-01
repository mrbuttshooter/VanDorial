# Changelog

## 2.3.0 — 2026-07-01

A hardening release: 22 adversarially-verified bugs fixed across three bug-hunt
waves (23 counting the approved call-path fixes), plus the GenCall 3.0
architecture roadmap. Test suite 323 → 353 (+30 regression tests). No breaking
changes; safe to deploy on the existing fleet.

### Highlights (deploy-critical)
- **Billed-minute accuracy** — long answered calls (30 min–2 h) were force-evicted
  mid-call and re-ingested as 0-second / code-0 records, silently *undercounting*
  answered minutes. `record_max_age_s` now tracks the answered ceiling (1800 →
  7500 default, in code and `gencall.cfg`) and `_persist` merges instead of
  clobbering.
- **PostgreSQL migration wedge** — `BOOLEAN DEFAULT 0` is rejected by Postgres, so
  migration `0007` aborted on every boot and left `loop_campaigns` without its
  profile columns → *every* campaign persist failed silently. Fixed to
  `DEFAULT FALSE` + a dialect rewrite so no future migration can hit it.
- **Shaper media_ip regression** — the diurnal shaper's hourly relaunch dropped
  `media_ip`, flipping the SDP media address to the signalling IP on the first
  curve step (the Algeria/Chad cause-47 one-way-audio failure). Now preserved.
  *(Call-path change — verify on-box with a working-vs-ours pcap before load.)*

### Fixed — call path (operator-approved; verify on-box before deploy)
- Shaper relaunch preserves `media_ip` (cause-47 regression).
- Reject `profile_enabled` + `target_calls` (a profiled campaign is minute-targeted).
- Adaptive-pool relaunch carries SIPp's `-m` count forward instead of resetting it
  (a call-count-targeted campaign no longer overshoots / never terminates).

### Fixed — control plane & API
- `/api/tests/start` now rejects (never clamps) out-of-cap rate/concurrency/duration.
- `RateLimiter` enforces limits above 1000 (was silently disabled by `deque(maxlen=1000)`).
- WebSocket handshake auth is read-only (no per-attempt DB write → reconnect-storm safe).
- `split_rate('total', …)` returns a clean 400 instead of dispatching rate 0.0 to trailing nodes.
- Cross-type fleet stop returns 409 instead of marking a run stopped while workers keep dialing.
- Single online-ness snapshot per node in the launch paths (no double-classify on flap).
- Fleet `completion_pct` divides by answered-out (matches the per-node matcher), not total attempts.

### Fixed — persistence, config & security
- `ConfigParser(interpolation=None)` — a literal `%` in a config value no longer crashes boot.
- Postgres DSN credentials are percent-encoded (special chars no longer corrupt the URL).
- Migration runner skips an `ADD COLUMN` whose column already exists (no more permanent wedge).
- Deleting a user / resetting a password now revokes that user's live browser sessions.
- Capture watchdog is a long-lived daemon (no TOCTOU leaving an uncapped, growing pcap).
- RTP media base-port is released on SIPp self-exit (no port-window leak over uptime).
- `StatsEngine._collect` iterates a snapshot (concurrent instance add/remove can't drop the snapshot).
- Adaptive-pool rebuild resolves the origin zone via the overlay + fuzzy match (was a silent no-op).

### Frontend
- Dashboard and Fleet render an explicit "Controller unreachable" state instead of a
  perpetual empty/loading view.

### Docs
- `docs/gencall-3.0/` — the GenCall 3.0 architecture roadmap and the three wave findings reports.

---

_Earlier releases tracked in git history (2.2.5–2.2.9 security/hardening waves)._

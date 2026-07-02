# Changelog

All notable changes to GenCall / VanDorial are documented here.
This project follows a pragmatic [SemVer](https://semver.org/): the MAJOR bump to
3.0.0 reflects the breadth of new operator-facing surface (metrics, alerts, RBAC,
CDR export, schedule windows, fleet stats push), not a break in the loop-runner
data path — **3.0.0 is a drop-in upgrade from 2.2.x** (see the upgrade notes in
[docs/deploy/release-3.0.md](docs/deploy/release-3.0.md)).

## [3.0.0] — 2026-07-02

### Added
- **Prometheus `/metrics`** on worker and controller (auth-gated, dependency-free)
  plus an importable **Grafana dashboard** (`deploy/grafana-gencall.json`) and a
  scrape guide (`docs/deploy/loop-runner.md` §7).
- **Operational webhook alerts** (`[alerts]`): HMAC-signed notifications for UAS
  restart, node online/offline, low loop completion, auto-resume, and startup
  stray-process kills.
- **Streamed CDR export**: `GET /api/loops/{id}/records.csv` (keyset-paginated;
  `since`/`until`/`direction` filters) and a **CDRs** download button on the
  History page.
- **JSON logging** option (`[logging] format = json`) — one object per line with
  per-record context fields, for journald/Loki/jq.
- **Console RBAC**: the `users.role` column now enforces `viewer` (read-only),
  `operator` (full operational), and `admin` (also account management). Machine
  API keys stay full-access. `gencall users create --role`.
- **Campaign schedule windows**: an optional daily active window per loop; the
  shaper pauses/resumes the dialer and the window survives a worker restart.
- **Worker → controller stats push** (`[fleet] controller_url`): workers push
  stats to the controller instead of being polled; the poll remains the
  fallback. Ingest is gated by a dedicated fleet-token check.
- **First-class per-node fleet-run rows** (`fleet_run_nodes`): normalized,
  queryable per-node run state alongside the legacy JSON blob.
- **Auth-gated OpenAPI** (`GET /api/openapi.json`) on worker + controller, with a
  build-time export (`gencall/scripts/export_openapi.py`) that generates the
  frontend's TypeScript types (`npm run generate:api`). CI fails on schema drift.

### Changed
- Packaging migrated from `setup.py` to a PEP 621 **`pyproject.toml`**; **ruff**
  adopted and enforced in CI.
- Call-record ingest now persists a whole parse pass in **one transaction**
  (was one commit per record).
- Controller startup/shutdown moved to a FastAPI **lifespan** context.
- `frontend/src/pages/Loops.tsx` split into focused components under
  `frontend/src/pages/loops/` (pure refactor).

### Fixed / hardened
- New DB index matching the loop matcher's actual inbound scan; a stale index
  dropped (migration `0008`).
- Removed dead console page and corrected the scenario catalog to the templates
  that actually ship.

### Offline / air-gapped install (self-contained)
- The `pyproject.toml` migration meant the install-time build is PEP 517/660 and
  needs a `setuptools >= 64`. This is satisfied **entirely offline** from either
  source: the bundled venv builder (`vendor/virtualenv.pyz`) already **seeds a
  modern setuptools from wheels embedded in the zipapp** (no network), and the
  wheelhouse now also ships `pip`/`setuptools`/`wheel` as belt-and-suspenders.
  `install-offline.sh` upgrades from the wheelhouse best-effort (`--no-index` —
  never the internet) then **verifies the setuptools capability** (from seed or
  wheelhouse) so a genuinely broken bundle fails with a clear message instead of
  a cryptic build error. Air-gapped boxes install 3.0.0 with **no PyPI and no
  GitHub**. `build-wheelhouse.sh` pins a dependency-free `wheel` to keep the
  wheelhouse self-contained. See
  [docs/deploy/release-3.0.md](docs/deploy/release-3.0.md) "Air-gapped boxes".

### Database migrations (applied automatically on boot)
- Worker: `0008_call_records_dir_created`, `0009_loop_campaign_schedule`
  (run by `apply_migrations` at startup).
- Controller: new `fleet_run_nodes` table (auto-created by `create_all`).
- **No manual migration steps.** See the upgrade notes for details.

### Security notes
- `/metrics`, `/api/openapi.json`, and the CDR export are all auth-gated.
- The worker→controller stats push uses a dedicated `X-Fleet-Token` check so the
  shared VLAN secret grants only stats ingest — never full controller control.
- The host firewall remains the real trust boundary (unchanged from 2.x).

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

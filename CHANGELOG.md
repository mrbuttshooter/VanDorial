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
- The `pyproject.toml` migration meant the PEP 517 build needs a build backend at
  install time. The offline bundle now ships `pip` + `setuptools` (>=64, PEP 660
  editable) + `wheel` in `vendor/wheelhouse/`, so **air-gapped boxes build and
  install 3.0.0 with no network** (no PyPI, no GitHub). `install-offline.sh`
  installs the backend strictly and fails loudly if a stale bundle lacks it;
  `build-wheelhouse.sh` pins a dependency-free `wheel` to keep the wheelhouse
  self-contained. See [docs/deploy/release-3.0.md](docs/deploy/release-3.0.md)
  "Air-gapped / no-internet boxes".

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

## [2.2.8] and earlier
See the git history; 2.2.x was the pre-3.0 loop-runner line.

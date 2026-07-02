# GenCall / VanDorial 3.0.0 — Deploy & Upgrade Guide

This is the release guide for **3.0.0**. It covers a fresh install, upgrading an
existing 2.2.x box, and turning on each of the new 3.0 features. The full
box-level deploy reference (firewall rules, validation checklist, monitoring)
stays in [loop-runner.md](loop-runner.md) — this document is the release wrapper
around it.

**3.0.0 is a drop-in upgrade from 2.2.x.** The loop-runner data path is
unchanged; all new surface is opt-in. DB migrations apply automatically on boot
and there are no manual schema steps.

---

## 0. What you're deploying

- **Worker** — REST API + SIPp UAC/UAS loop engine. One per origination box.
- **Controller** — fleet console + multi-node aggregation (`:8090`). One per fleet.
- **PostgreSQL** — campaigns, per-call records, loop stats.

Both roles ship from the same codebase; the role is chosen at install time.

---

## 1. Fresh install (Ubuntu, native — recommended)

```bash
# 1. get the 3.0.0 code on the box
git clone https://github.com/mrbuttshooter/VanDorial.git
cd VanDorial
git checkout 3.0.0            # the release tag

# 2. run the installer (apt deps, builds SIPp, PostgreSQL, venv, systemd unit)
chmod +x deploy/*.sh
sudo ./deploy/install-ubuntu.sh          # interactive: asks role + MADA whitelist
#   non-interactive:  sudo ROLE=worker ./deploy/install-ubuntu.sh
#                     sudo ROLE=controller ./deploy/install-ubuntu.sh

# 3. firewall — the REAL trust boundary (nftables/ufw rules in loop-runner.md §2)
```

The installer prints the **API key** (also saved to `/opt/gencall/.api_key`) and
starts the `gencall-worker` systemd service. DB migrations apply at boot
("DB migrations apply automatically at boot" in the installer output).

- Worker health: `curl -H "X-API-Key: <key>" http://localhost:8080/api/health`
- Controller console (if role=controller): `http://<box-ip>:8080/console/`
- Logs: `journalctl -u gencall-worker -f` (or `gencall-controller`)

Docker path: `./deploy/install.sh` then `./deploy/smoke-loopback.sh`.

### Air-gapped / no-internet boxes (most IPs)

Boxes with no internet can't `git clone` or reach PyPI, so 3.0.0 ships a
**fully self-contained offline bundle** — nothing is downloaded at install time:

- `vendor/wheelhouse/` — every Python dependency, **plus** `pip`/`setuptools`/
  `wheel`. 3.0.0 moved packaging to `pyproject.toml`, so the install builds the
  app with PEP 517/660 (`setuptools >= 64`). That setuptools is available
  **entirely offline** from either the venv builder's embedded seed (see below)
  or the wheelhouse — nothing is fetched.
- `vendor/virtualenv.pyz` — the venv builder, which **embeds** its `pip`/
  `setuptools` seed wheels, so it creates the venv (with a modern setuptools)
  with no OS `python3-venv` and no network.
- `vendor/debs/` — SIPp (`sip-tester` + libs), dpkg-installed if `sipp` is absent.
- `gencall/web/console/` — the console is **prebuilt** (no `npm`/Node on the box).

**Workflow:** build/refresh the bundle once on an *online* box whose Python
matches the air-gapped targets (Ubuntu 22.04 / Python 3.10), tar it, copy it to
each air-gapped box, and run the offline installer:

```bash
# on an ONLINE box, same Python as the targets (only needed to (re)build the bundle):
git clone https://github.com/mrbuttshooter/VanDorial.git && cd VanDorial
git checkout 3.0.0
PYTHON=python3.10 ./deploy/build-wheelhouse.sh     # refresh vendor/wheelhouse
./deploy/build-debs.sh                             # refresh vendor/debs (SIPp)
tar czf gencall-3.0.0-offline.tgz --exclude='.git' .

# copy gencall-3.0.0-offline.tgz to each AIR-GAPPED box, then:
tar xzf gencall-3.0.0-offline.tgz && cd VanDorial
sudo ./deploy/install-offline.sh                   # ROLE=worker|controller, no network
```

The offline installer verifies the wheelhouse Python tag matches the box and
that a PEP 517-capable `setuptools` (>=64) is available **from either the venv
builder's embedded seed or the wheelhouse** — so a genuinely broken bundle is
caught immediately with a clear message, not at a cryptic build error. Every
`pip` step uses `--no-index` (never the internet). If you already run 2.2.x from
an unpacked bundle, upgrading is the same: drop in the 3.0.0 bundle and re-run
`install-offline.sh` (migrations auto-apply).

> The committed release tag already contains a ready-to-use wheelhouse for
> Ubuntu 22.04 / Python 3.10. You only need `build-wheelhouse.sh` if your boxes
> run a different Python/OS.

---

## 2. Upgrade an existing 2.2.x box → 3.0.0

Nothing about the loop data path changes; this is code + auto-migration only.

```bash
cd /path/to/VanDorial          # the checkout the box runs from
sudo systemctl stop gencall-worker            # (and gencall-controller if used)

git fetch --tags origin
git checkout 3.0.0

# reinstall the package into the existing venv (PEP 517 build)
sudo /opt/gencall/venv/bin/pip install -e /path/to/VanDorial
#   air-gapped: sudo /opt/gencall/venv/bin/pip install --no-index \
#               --find-links vendor/wheelhouse -e /path/to/VanDorial

# rebuild the console bundle only if you serve it from source (the release
# already ships the built console under gencall/web/console/)

sudo systemctl start gencall-worker           # migrations 0008/0009 apply on boot
```

**What happens automatically on the first 3.0 boot:**
- Worker: `apply_migrations` runs `0008_call_records_dir_created` and
  `0009_loop_campaign_schedule` (idempotent; tracked in `schema_migrations`).
- Controller: `create_all` provisions the new `fleet_run_nodes` table.
- Existing API keys, users, campaigns, and records are untouched.

**Verify the upgrade:**
```bash
curl -H "X-API-Key: <key>" http://localhost:8080/api/health   # "version":"3.0.0"
journalctl -u gencall-worker -n 30 --no-pager | grep -i "migration\|v3.0.0"
```

Rollback is `git checkout <old-tag>` + `pip install -e .` + restart; the new
columns/table are additive and harmless to older code.

---

## 3. Turn on the new 3.0 features (all opt-in)

All of these live in `/opt/gencall/etc/gencall.cfg` unless noted. Restart the
service after editing (`systemctl restart gencall-worker`).

### 3.1 Prometheus metrics + Grafana
`GET /metrics` is already live on worker and controller, auth-gated with the same
`X-API-Key`. Mint a read-only scrape key and point Prometheus at it:
```bash
gencall keys create --name prometheus       # a viewer-scoped key is enough
```
Scrape config + dashboard import: **loop-runner.md §7**. Dashboard file:
`deploy/grafana-gencall.json`.

### 3.2 Operational webhook alerts — `[alerts]`
```ini
[alerts]
webhook_url = https://hooks.example.com/gencall
webhook_secret = <hmac-secret>        # signs the body: X-GenCall-Signature
events =                              # empty = all; or e.g. node_offline uas_restarted
min_interval_s = 60
completion_min_pct = 0                # >0 alerts when a running loop drops below it
```

### 3.3 JSON logging — `[logging]`
```ini
[logging]
format = json                         # default 'text'
```

### 3.4 Console RBAC (viewer / operator / admin)
```bash
gencall users create alice --role viewer      # read-only console
gencall users create bob   --role operator    # full operational control
gencall users create carol --role admin        # + account management
```
Viewers can view everything but issue no state-changing calls; only admins (and
machine API keys) manage accounts. Existing users default to `operator`.

### 3.5 Campaign schedule windows
Per-campaign, on the loop start request (`POST /api/loops`):
`schedule_enabled`, `schedule_start_min`, `schedule_end_min`,
`schedule_tz_offset` (minutes since local midnight; `start == end` = always on;
`start > end` wraps midnight). The dialer pauses/resumes automatically and the
window survives a restart.

### 3.6 Worker → controller stats push — `[fleet]`
On each **worker** that should push instead of being polled:
```ini
[fleet]
token = <same shared VLAN secret on every box>
controller_url = http://<controller-ip>:8090      # empty = push off (poll fallback)
node_address = http://<this-worker-ip>:8080       # how the controller keys this node
```
The controller accepts the push at `POST /api/fleet/ingest/stats`, gated by
`X-Fleet-Token` (= `token`). If the push stops, the controller's 1 Hz poll
resumes as the fallback — nothing to configure for that.

### 3.7 CDR export
`GET /api/loops/{id}/records.csv?since=&until=&direction=` streams a campaign's
per-call records (auth-gated). In the console: the **CDRs** button on the History
page. `since`/`until` accept an ISO date (whole day) or datetime; `direction` is
`out` or `in`.

---

## 4. Post-deploy validation

Run the box checklist in **loop-runner.md §6** (CPU under load, no fd growth, no
orphaned SIPp, completion % tracks MADA). Then spot-check the 3.0 surface you
enabled:

- `curl -H "X-API-Key: <key>" http://localhost:8080/metrics | head`
- `gencall users list` shows the roles you created
- if pushing: on the controller, `GET /api/fleet/stats` reflects a worker you did
  NOT poll (check `journalctl -u gencall-controller` for ingest)

---

## 5. Rollback

```bash
sudo systemctl stop gencall-worker
git checkout <previous-tag>            # e.g. v2.2.8 history
sudo /opt/gencall/venv/bin/pip install -e .
sudo systemctl start gencall-worker
```
The 3.0 migrations only add an index + nullable columns + a new controller table,
so older code runs fine against the upgraded schema.

# GenCall v2 — VanDorial Loop Runner

An in-house SIP call generator and **minutes-for-minutes loop runner**: it originates
outbound calls, answers the leg that returns through your switch, and reports the loop
(minutes out vs in, completion %, ASR/ACD, failures by SIP code). Native **SIPp** does the
call/media work; Python is control-plane only, so it idles near 0 % CPU.

- **Worker** — REST/WebSocket API + SIPp UAC (dialers) + a persistent UAS (answer side).
- **Controller** — fleet console + multi-node aggregation (`:8090`).
- **PostgreSQL** — campaigns, per-call records, loop stats.

The loop SIPp scenarios live in [`gencall/scenarios/templates/`](gencall/scenarios/templates/)
(`loop_uac.xml`, `loop_uas.xml`) and are used automatically by the engine — nothing to install
separately.

---

## Install on Ubuntu (4 vCPU / 4 GB / 40 GB target)

### Native install (recommended — no Docker, systemd + apt)

Runs straight on the host like sigma: PostgreSQL + SIPp + a systemd service. Needs UDP/5060
free (don't co-locate with sigma unless you change GenCall's SIP port).

```bash
# 1. get the code on the box (clone, or unzip the release)
git clone https://github.com/mrbuttshooter/VanDorial.git
cd VanDorial

# 2. native installer: apt deps, builds SIPp from source, sets up PostgreSQL,
#    installs into /opt/gencall (venv), writes config, starts the systemd worker
chmod +x deploy/*.sh
sudo ./deploy/install-ubuntu.sh

# 3. firewall (the REAL trust boundary): restrict UDP/5060 + the RTP range to your
#    MADA whitelist — rules in docs/deploy/loop-runner.md section 2 (nftables/ufw)
```

It's interactive and idempotent. It asks:

- **Role — worker or controller.** A **worker** runs headless (REST API + loop engine, **no
  dashboard / web app**) and is driven from a controller. A **controller** serves the full
  console / web app on `:8080`. Set non-interactively with `ROLE=worker` / `ROLE=controller`.
- the MADA whitelist (generates a DB password automatically).

When it finishes it **prints the API key** (the `X-API-Key:` header value, also saved to
`/opt/gencall/.api_key`) — use it to register a worker on the controller's **Nodes** page, or to
call the API. Controller dashboard + **Loops** page: `http://<box-ip>:8080/console/`.
Logs: `journalctl -u gencall-worker -f`. Air-gapped box? Use `sudo ./deploy/install-offline.sh`
(same role prompt + key). It's **fully self-contained** — the bundle ships SIPp (`vendor/debs/`),
the venv builder (`vendor/virtualenv.pyz`) and every Python lib (`vendor/wheelhouse/`), so the
**only** box prerequisite is `python3` (present on Ubuntu by default). The bundled binaries target
Ubuntu 22.04 / Python 3.10; for a different OS/Python, refresh them on a matching online box with
`deploy/build-debs.sh` + `deploy/build-wheelhouse.sh`.

### Docker install (alternative)

If you'd rather run it containerised (Docker Engine + Compose v2):

```bash
git clone https://github.com/mrbuttshooter/VanDorial.git && cd VanDorial
chmod +x deploy/*.sh
./deploy/install.sh            # builds the image, starts postgres/worker/controller
./deploy/smoke-loopback.sh     # proves the real-SIPp call path (Docker)
```

Full step-by-step, firewall rules, and the post-deploy validation checklist for both paths
are in **[docs/deploy/loop-runner.md](docs/deploy/loop-runner.md)**.

---

## What's verified vs. what to confirm on the box

Verified in CI (152 tests, Postgres-dialect + a stub SIPp): the records pipeline, RFC-3261
duration math, LoopEngine/LoopMatcher, reliability (no orphaned dialers, restart recovery),
and the API/console.

Confirm **on the box** (needs real SIPp + Docker — can't be faked off-Linux):
`./deploy/smoke-loopback.sh` (scenarios load on real SIPp + a call completes end-to-end),
the image building, and a 50-loop / 1-hour run for the CPU/fd/orphan envelope. See
[docs/deploy/loop-runner.md](docs/deploy/loop-runner.md) §5.

---

## Repo layout

| Path | What |
|---|---|
| `gencall/` | Worker + controller (FastAPI), core engine, SIPp scenarios, DB migrations |
| `frontend/` | React/Vite NOC console (prebuilt into `gencall/web/console/`) |
| `deploy/` | `install.sh`, `smoke-loopback.sh` |
| `docs/deploy/loop-runner.md` | Ubuntu deploy guide + firewall + validation checklist |
| `docker-compose.v2.yml` | The v2 stack (host networking for the worker) |
| `sigma-re/patch/` | (Bonus) reversible CPU hotfix for the legacy "sigma" build — unrelated to GenCall |

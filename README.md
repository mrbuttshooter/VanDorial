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

Prerequisites: Ubuntu 22.04+, Docker Engine + Compose v2 (`docker compose version`).

```bash
# 1. get the code on the box (clone, or unzip the release)
git clone https://github.com/mrbuttshooter/VanDorial.git
cd VanDorial

# 2. guided install: sets up .env + gencall.cfg, builds the SIPp image,
#    starts postgres -> worker -> controller, runs health checks
chmod +x deploy/*.sh
./deploy/install.sh

# 3. apply the firewall (the REAL trust boundary) — restrict UDP/5060 + the RTP
#    range to your MADA whitelist. Rules are in:
#       docs/deploy/loop-runner.md  (section 2: nftables AND ufw)

# 4. prove the real-SIPp call path on the box
./deploy/smoke-loopback.sh
```

`./deploy/install.sh` is interactive (asks for the Postgres password and your MADA
whitelist IPs) and idempotent — safe to re-run. Full step-by-step, firewall rules, and the
post-deploy validation checklist are in **[docs/deploy/loop-runner.md](docs/deploy/loop-runner.md)**.

Open the console at `http://<box>:8090/console/` → the **Loops** page.

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

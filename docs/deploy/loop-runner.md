# GenCall v2 Loop Runner — single-box deploy

Deploy guide for the **4 vCPU / 4 GB / 40 GB** Ubuntu box that runs one GenCall
worker (SIPp UAC + persistent UAS), the fleet controller, and PostgreSQL.

This is the loop-runner topology from the design spec
(`docs/superpowers/specs/2026-06-10-gencall-loop-runner-design.md`, §4.5 / §7
stage 10). It uses the **v2 compose file** (`docker-compose.v2.yml`) with **host
networking** for the worker — Docker's userland proxy spawns one process per
published port, and publishing the RTP range (thousands of UDP ports) OOMs a
small host. Host networking puts SIPp's signalling + media straight on the host
NIC with no per-port proxy.

Because the worker is on host networking, **the host firewall is the real trust
boundary** (design §4.1). Docker does not gate inbound SIP/RTP here — nftables
or ufw does. The app layer only *verifies* (it tags each inbound call with its
`source_ip` and flags anything outside the whitelist, so a misconfigured
firewall is visible), it does not enforce. Get the firewall right.

---

## 0. Prerequisites

- Ubuntu 22.04+ with Docker Engine + Compose v2 (`docker compose version`).
- The box's public/SIP-facing interface name (examples below use `eth0` — set
  `IFACE` to yours).
- MADA's signalling IP(s). This is the whitelist. Everything else is denied.

Pick a **conservative, well-known RTP range** and use it consistently in three
places (they MUST match):

1. `docker-compose.v2.yml` → `RTP_PORT_RANGE`
2. `gencall/etc/gencall.cfg` → `[sip] min_rtp_port` / `max_rtp_port`
3. the firewall rules below

The examples use **`16384-16584`** (≈ 200 ports ≈ 100 concurrent two-way calls,
matching the 4 GB caps in `[loops]`). Raise it only after measuring on the box.

---

## 1. Configure

```bash
cp .env.example .env            # set POSTGRES_PASSWORD (required)
$EDITOR .env
```

In `.env`:

```ini
POSTGRES_PASSWORD=<a-strong-password>
RTP_PORT_RANGE=16384-16584
```

In `gencall/etc/gencall.cfg`:

```ini
[sip]
min_rtp_port = 16384
max_rtp_port = 16584

[trust]
# MADA signalling IP(s) — the inbound whitelist. Comma/space separated.
whitelist = 203.0.113.10, 203.0.113.11

[retention]
call_records_days = 30
interval_hours = 24
```

Keep `[trust] whitelist` and the firewall rules (below) in sync — they describe
the same trust boundary from two layers.

---

## 2. Firewall — the real trust boundary

Default-deny inbound; allow UDP/5060 (SIP) **and** the RTP range **only** from
the MADA whitelist. Apply **one** of the rule sets below (nftables is preferred
on modern Ubuntu; ufw is the simpler alternative). Do not run both.

Set the shared variables first:

```bash
IFACE=eth0
RTP_LO=16384
RTP_HI=16584
MADA_IPS="203.0.113.10 203.0.113.11"   # space-separated whitelist
ADMIN_CIDR=198.51.100.0/24             # where you administer the box from (SSH + console)
```

### 2a. nftables

`/etc/nftables.conf` (or a drop-in). This is a complete, default-deny inbound
ruleset; adjust the management allowances to your environment.

```nft
#!/usr/sbin/nft -f
flush ruleset

define IFACE       = eth0
define RTP_LO      = 16384
define RTP_HI      = 16584
define MADA        = { 203.0.113.10, 203.0.113.11 }   # MADA signalling IPs
define ADMIN_CIDR  = 198.51.100.0/24                  # SSH + console admin source

table inet filter {
    chain input {
        type filter hook input priority filter; policy drop;

        # Stateful baseline.
        ct state established,related accept
        ct state invalid drop
        iif "lo" accept
        ip protocol icmp accept
        ip6 nexthdr icmpv6 accept

        # Management: SSH + the controller console, from the admin network only.
        ip saddr $ADMIN_CIDR tcp dport { 22, 8090 } accept

        # SIP signalling (UDP/5060) — MADA whitelist only. Add `tcp dport 5060`
        # too if you run SIP/TCP.
        iifname $IFACE ip saddr $MADA udp dport 5060 accept

        # RTP media range — MADA whitelist only.
        iifname $IFACE ip saddr $MADA udp dport $RTP_LO-$RTP_HI accept

        # Everything else inbound is dropped by the chain policy above.
    }

    chain forward { type filter hook forward priority filter; policy drop; }
    chain output  { type filter hook output  priority filter; policy accept; }
}
```

Apply and persist:

```bash
sudo nft -f /etc/nftables.conf
sudo systemctl enable --now nftables
sudo nft list ruleset            # verify
```

### 2b. ufw (alternative)

ufw has no native multi-port range + per-source one-liner that reads cleanly, so
loop over the whitelist. Run as root:

```bash
# Default-deny inbound, allow outbound.
ufw default deny incoming
ufw default allow outgoing

# Management from the admin network only.
ufw allow from "$ADMIN_CIDR" to any port 22 proto tcp
ufw allow from "$ADMIN_CIDR" to any port 8090 proto tcp

# SIP + RTP from each MADA IP only.
for ip in $MADA_IPS; do
    ufw allow from "$ip" to any port 5060 proto udp
    ufw allow from "$ip" to any port "$RTP_LO":"$RTP_HI" proto udp
done

ufw --force enable
ufw status verbose               # verify
```

> Note: Docker normally bypasses ufw by writing iptables rules directly — but
> the worker here is on **host networking**, so its sockets are ordinary host
> sockets that ufw/nftables filter normally. The only published port is the
> controller's 8090 (bridge), already covered above. Keep Postgres bound to
> `127.0.0.1` (the v2 compose does) so it is never reachable off-box.

---

## 3. Build and start

Docker/Linux only — the image builds SIPp from source (UDP/TCP/TLS, no SCTP):

```bash
docker compose -f docker-compose.v2.yml build
docker compose -f docker-compose.v2.yml up -d postgres
# wait for Postgres to accept connections, then:
docker compose -f docker-compose.v2.yml up -d gencall controller
```

DB migrations (including `call_records` and the retention gate) apply
automatically at worker startup.

Sanity checks:

```bash
docker compose -f docker-compose.v2.yml exec gencall sipp -v   # SIPp present
curl -fsS http://127.0.0.1:8080/api/health                     # worker health
curl -fsS http://127.0.0.1:8090/api/health                     # controller health
```

---

## 4. Retention

`call_records` is the growth table (≈ 24k rows/day/direction at 50 concurrent
loops). The built-in retention job prunes it — **interval-gated**, at most once
per `[retention] interval_hours` (default 24 h), deleting rows older than
`[retention] call_records_days` (default 30). The gate timestamp is persisted in
`retention_runs`, so a restart loop can never trigger a per-boot DELETE storm.
No cron job is required; set `call_records_days = 0` to disable pruning.

---

## 5. Box validation checklist (design §6)

After a deploy, run a 50-loop campaign for ~1 h from the console and confirm:

- GenCall (Python) CPU stays under ~5 % (native SIPp does the call/media work).
- No file-descriptor growth on the worker process over the hour.
- No orphaned `sipp` processes after a campaign stop, a worker kill, or a reboot
  (startup reconciliation kills stale PIDs from `managed_processes`).
- Completion % on the Loops page tracks MADA's own returned-call counters.
- Every inbound `call_record` carries a `source_ip` inside the whitelist (if any
  are flagged outside it, the firewall and `[trust] whitelist` are out of sync).

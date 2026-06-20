# Controller-Managed Trust Whitelist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** Stop prompting for the app-layer trust whitelist at install; let an operator set it from the controller and push it to all workers at runtime (and re-push when a worker rejoins). The host firewall stays a manual ops step (the real boundary) — out of scope here.

**Architecture:** The worker's `CallRecordParser` already holds `trust_whitelist`/`drop_untrusted` (with a `_lock`); add a thread-safe setter + a worker `GET/POST /api/config/trust`. The controller persists a singleton `FleetSettings` row and a `POST /api/fleet/config/trust` that saves it and fans out to all enabled nodes via `NodeClient` (same pattern as fleet loop launch); the health-poll loop re-pushes to a node that transitions offline→online. A Config-page panel drives it. Empty list = allow-all (existing semantics); `enabled=false` pushes an empty list while preserving the saved list.

**Tech Stack:** Python/FastAPI/SQLAlchemy, httpx (async NodeClient), pytest; React/TS frontend.

All file:line anchors verified by grep on 2026-06-20.

---

## Task 1: Thread-safe trust setter on `CallRecordParser`

**Files:**
- Modify: `gencall/core/call_records.py`
- Test: `tests/test_call_records.py`

- [ ] **Step 1: Failing test**

Append to `tests/test_call_records.py`:

```python
def test_set_and_get_trust_is_threadsafe_swap():
    p = CallRecordParser(db=None, trust_whitelist=["10.0.0.1"], drop_untrusted=False)
    assert p.get_trust() == {"ips": ["10.0.0.1"], "drop_untrusted": False}
    p.set_trust(["10.0.0.2", "192.168.0.0/24"], True)
    assert p.get_trust() == {"ips": ["10.0.0.2", "192.168.0.0/24"], "drop_untrusted": True}
    # empty list = allow-all
    p.set_trust([], False)
    assert ip_in_whitelist("8.8.8.8", p.trust_whitelist) is True
```

- [ ] **Step 2: Run → fails** (`AttributeError: 'CallRecordParser' object has no attribute 'set_trust'`)

Run: `python -m pytest tests/test_call_records.py -k set_and_get_trust -v`

- [ ] **Step 3: Implement** — add to `CallRecordParser` (near `start()`):

```python
def set_trust(self, whitelist, drop_untrusted):
    """Hot-swap the inbound trust config (controller push). Thread-safe vs the
    tail-poll thread: we replace the list REFERENCE under the lock, so a reader
    in _apply_trust_filter always sees a complete old-or-new list."""
    with self._lock:
        self.trust_whitelist = list(whitelist or [])
        self.drop_untrusted = bool(drop_untrusted)

def get_trust(self):
    """Current effective trust config."""
    with self._lock:
        return {"ips": list(self.trust_whitelist), "drop_untrusted": self.drop_untrusted}
```

No change to `_apply_trust_filter` is needed: it reads `self.trust_whitelist` (a reference) then iterates an immutable list; `set_trust` swaps the reference atomically under the lock.

- [ ] **Step 4: Run → passes.** Then `python -m pytest tests/test_call_records.py -q` (green except the 2 known pre-existing E.164 failures elsewhere don't run here).

- [ ] **Step 5: Commit**

```bash
git add gencall/core/call_records.py tests/test_call_records.py
git commit -m "feat(trust): thread-safe set_trust/get_trust on CallRecordParser"
```

---

## Task 2: Worker endpoints `GET/POST /api/config/trust`

**Files:**
- Modify: `gencall/api/loops.py` (has the `call_parser` module global + the mounted `router`)
- Test: `tests/test_sale_zones.py` (reuse its TestClient style) or a new `tests/test_trust_config.py`

- [ ] **Step 1: Failing test** — create `tests/test_trust_config.py`:

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gencall.api import loops as loops_mod
from gencall.api import routes as routes_mod
from gencall.core.call_records import CallRecordParser


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.dependency_overrides = {}
    parser = CallRecordParser(db=None, trust_whitelist=[], drop_untrusted=False)
    loops_mod.call_parser = parser
    app.include_router(loops_mod.router)
    from gencall.api.routes import require_api_key
    app.dependency_overrides[require_api_key] = lambda: None
    return TestClient(app), parser


def test_get_then_post_trust(client):
    c, parser = client
    assert c.get("/api/config/trust").json() == {"ips": [], "drop_untrusted": False}
    r = c.post("/api/config/trust", json={"ips": ["10.0.0.1", "192.168.0.0/24"], "drop_untrusted": True})
    assert r.status_code == 200, r.text
    assert parser.get_trust() == {"ips": ["10.0.0.1", "192.168.0.0/24"], "drop_untrusted": True}
    assert c.get("/api/config/trust").json()["drop_untrusted"] is True


def test_post_trust_rejects_bad_ip(client):
    c, _ = client
    assert c.post("/api/config/trust", json={"ips": ["not-an-ip"], "drop_untrusted": False}).status_code == 422
```

- [ ] **Step 2: Run → fails** (404).

Run: `python -m pytest tests/test_trust_config.py -v`

- [ ] **Step 3: Implement** — in `gencall/api/loops.py`, add after the sale-zone endpoints:

```python
class TrustConfigBody(BaseModel):
    ips: list[str] = []
    drop_untrusted: bool = False


@router.get("/api/config/trust", dependencies=[Depends(require_api_key)])
def get_trust_config():
    """This worker's current inbound trust whitelist + drop flag."""
    if call_parser is None:
        raise HTTPException(503, "call-record parser not configured on this worker")
    return call_parser.get_trust()


@router.post("/api/config/trust", dependencies=[Depends(require_api_key)])
def set_trust_config(body: TrustConfigBody):
    """Hot-apply an inbound trust whitelist (controller push). Empty ips = allow-all."""
    import ipaddress
    if call_parser is None:
        raise HTTPException(503, "call-record parser not configured on this worker")
    for tok in body.ips:
        try:
            ipaddress.ip_network((tok or "").strip(), strict=False)
        except ValueError:
            raise HTTPException(422, f"invalid IP/CIDR: {tok!r}")
    call_parser.set_trust([t.strip() for t in body.ips if t.strip()], body.drop_untrusted)
    return {"status": "applied", **call_parser.get_trust()}
```

(`call_parser`, `BaseModel`, `Depends`, `HTTPException`, `require_api_key` are already in this module.)

- [ ] **Step 4: Run → passes.** Then `python -m pytest tests/test_trust_config.py -q`.

- [ ] **Step 5: Commit**

```bash
git add gencall/api/loops.py tests/test_trust_config.py
git commit -m "feat(trust): worker GET/POST /api/config/trust (hot-apply)"
```

---

## Task 3: Controller `FleetSettings` singleton + get/set

**Files:**
- Modify: `gencall/controller/models.py` (add model + DB accessor methods, matching its existing `Base`/DB-class pattern — the file already defines `Node`, `Group`, `FleetRun` and a controller Database class with `get_session()`/`create_tables()`)
- Test: `tests/test_controller.py`

- [ ] **Step 1: Failing test** — append to `tests/test_controller.py` (match its existing controller-DB fixture; if none, instantiate the controller Database on a `sqlite:///{tmp_path}` and call `create_tables()`):

```python
def test_fleet_settings_singleton_get_set(tmp_path):
    from gencall.controller.models import ControllerDatabase  # use the actual class name in this file
    db = ControllerDatabase(f"sqlite:///{tmp_path / 'ctl.db'}")
    db.create_tables()
    # default
    assert db.get_fleet_trust() == {"enabled": False, "ips": [], "drop_untrusted": False}
    db.set_fleet_trust(enabled=True, ips=["10.0.0.1", "10.0.0.2"], drop_untrusted=True)
    assert db.get_fleet_trust() == {"enabled": True, "ips": ["10.0.0.1", "10.0.0.2"], "drop_untrusted": True}
    # singleton: a second set updates the same row, not a new one
    db.set_fleet_trust(enabled=False, ips=[], drop_untrusted=False)
    assert db.get_fleet_trust()["enabled"] is False
```

> Use the controller DB class's REAL name from `gencall/controller/models.py` (it may be `ControllerDatabase` or `Database`); adjust the import accordingly.

- [ ] **Step 2: Run → fails.**

Run: `python -m pytest tests/test_controller.py -k fleet_settings -v`

- [ ] **Step 3: Implement** — add to `gencall/controller/models.py` (using its existing `Base` and `datetime` imports):

```python
class FleetSettings(Base):
    """Fleet-wide runtime settings (singleton row id=1). Today: the inbound
    trust whitelist pushed to every worker."""
    __tablename__ = "fleet_settings"

    id = Column(Integer, primary_key=True)          # always 1 (singleton)
    trust_enabled = Column(Boolean, default=False)
    trust_whitelist = Column(Text, default="")      # space/comma-separated IPs/CIDRs
    trust_drop_untrusted = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)
```

Add these methods to the controller Database class (the one with `get_session()`):

```python
def get_fleet_trust(self) -> dict:
    session = self.get_session()
    try:
        row = session.query(FleetSettings).filter_by(id=1).first()
        if row is None:
            return {"enabled": False, "ips": [], "drop_untrusted": False}
        ips = [t for t in (row.trust_whitelist or "").replace(",", " ").split() if t]
        return {"enabled": bool(row.trust_enabled), "ips": ips,
                "drop_untrusted": bool(row.trust_drop_untrusted)}
    finally:
        session.close()

def set_fleet_trust(self, enabled: bool, ips: list, drop_untrusted: bool) -> None:
    session = self.get_session()
    try:
        row = session.query(FleetSettings).filter_by(id=1).first()
        if row is None:
            row = FleetSettings(id=1)
            session.add(row)
        row.trust_enabled = bool(enabled)
        row.trust_whitelist = " ".join(t.strip() for t in ips if t and t.strip())
        row.trust_drop_untrusted = bool(drop_untrusted)
        session.commit()
    finally:
        session.close()

def effective_fleet_ips(self) -> list:
    """The list workers should enforce: the saved list when enabled, else []
    (allow-all) — keeps the saved list for the UI while enforcement is off."""
    t = self.get_fleet_trust()
    return t["ips"] if t["enabled"] else []
```

Ensure `Text`, `Boolean`, `DateTime`, `Integer`, `Column` and `datetime` are imported in this file (add to its import line if missing).

- [ ] **Step 4: Run → passes.**

- [ ] **Step 5: Commit**

```bash
git add gencall/controller/models.py tests/test_controller.py
git commit -m "feat(trust): controller FleetSettings singleton + get/set/effective"
```

---

## Task 4: `NodeClient.set_trust_whitelist` + controller fan-out endpoint

**Files:**
- Modify: `gencall/controller/node_client.py`
- Modify: `gencall/controller/routes.py` (where the other `/api/fleet/*` controller endpoints live, with access to `db` and the node list)
- Test: `tests/test_controller.py`

- [ ] **Step 1: Add the NodeClient method** — in `gencall/controller/node_client.py`, after `get_loop`:

```python
async def set_trust_whitelist(self, ips: list, drop_untrusted: bool) -> dict:
    """POST /api/config/trust on the worker (push the inbound trust config)."""
    return await self._request_json(
        "POST", "/api/config/trust",
        json={"ips": ips, "drop_untrusted": drop_untrusted})
```

- [ ] **Step 2: Failing test** for the controller endpoints — append to `tests/test_controller.py`. Mock the per-node push so no network is needed (match how other controller tests build the app/TestClient; monkeypatch `NodeClient.set_trust_whitelist`):

```python
def test_fleet_trust_endpoint_saves_and_fans_out(monkeypatch, tmp_path):
    # Build the controller app the same way the other controller tests do.
    # (See the existing controller TestClient fixture/helper in this file.)
    pushed = []

    async def fake_push(self, ips, drop_untrusted):
        pushed.append((self.address, list(ips), drop_untrusted))
        return {"status": "applied", "ips": ips, "drop_untrusted": drop_untrusted}

    from gencall.controller.node_client import NodeClient
    monkeypatch.setattr(NodeClient, "set_trust_whitelist", fake_push, raising=True)

    client, ctl_db = _make_controller_client(tmp_path)   # reuse the file's helper
    _add_enabled_node(ctl_db, address="http://w1:8000", api_key="k1")  # reuse/inline

    r = client.post("/api/fleet/config/trust",
                    json={"enabled": True, "ips": ["10.0.0.1"], "drop_untrusted": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert any(res["address"] == "http://w1:8000" and res["ok"] for res in body["results"])
    assert ("http://w1:8000", ["10.0.0.1"], True) in pushed
    assert ctl_db.get_fleet_trust()["ips"] == ["10.0.0.1"]
```

> If `tests/test_controller.py` lacks reusable helpers like `_make_controller_client`/`_add_enabled_node`, build the app + an enabled `Node` row inline following the existing tests in that file.

- [ ] **Step 3: Implement the controller endpoints** — in `gencall/controller/routes.py`, add (matching how the file accesses the controller `db`, builds `NodeClient` per node, and enumerates enabled `Node` rows — see the fleet loop-launch handler and `_enabled_node_provider` in `controller/app.py`):

```python
from pydantic import BaseModel

class FleetTrustBody(BaseModel):
    enabled: bool = False
    ips: list[str] = []
    drop_untrusted: bool = False


@app.get("/api/fleet/config/trust", dependencies=[Depends(require_api_key)])
def get_fleet_trust():
    if db is None:
        raise HTTPException(500, "controller DB not configured")
    return db.get_fleet_trust()


@app.post("/api/fleet/config/trust", dependencies=[Depends(require_api_key)])
async def set_fleet_trust(body: FleetTrustBody):
    """Persist the fleet trust whitelist and push it to every enabled worker.
    Pushes the saved list when enabled, else an empty (allow-all) list."""
    if db is None:
        raise HTTPException(500, "controller DB not configured")
    # validate IPs
    import ipaddress
    for tok in body.ips:
        try:
            ipaddress.ip_network((tok or "").strip(), strict=False)
        except ValueError:
            raise HTTPException(422, f"invalid IP/CIDR: {tok!r}")
    db.set_fleet_trust(body.enabled, body.ips, body.drop_untrusted)
    effective = db.effective_fleet_ips()

    nodes = _enabled_nodes()            # use the file's existing enabled-node enumerator
    results = []
    for n in nodes:
        addr = n["address"]; key = n.get("api_key", "")
        client = NodeClient(addr, key, verify=False)
        try:
            await client.set_trust_whitelist(effective, body.drop_untrusted)
            results.append({"address": addr, "ok": True, "error": None})
        except Exception as e:
            results.append({"address": addr, "ok": False, "error": str(e)})
    return {**db.get_fleet_trust(), "pushed": len(results), "results": results}
```

> Use the controller's actual enabled-node enumerator (the `Node`-querying helper this file or `controller/app.py` already uses; the Explore identified `_enabled_node_provider`). If `routes.py` uses an `APIRouter` named `router` instead of `app`, use that; match the file.

- [ ] **Step 4: Run → passes.** `python -m pytest tests/test_controller.py -q`.

- [ ] **Step 5: Commit**

```bash
git add gencall/controller/node_client.py gencall/controller/routes.py tests/test_controller.py
git commit -m "feat(trust): controller /api/fleet/config/trust persists + fans out to workers"
```

---

## Task 5: Re-push on worker (re)join

**Files:**
- Modify: `gencall/controller/aggregator.py`
- Test: `tests/test_controller.py` (or `tests/test_fleet_discovery.py`)

- [ ] **Step 1: Implement the re-push hook** — in `FleetAggregator._poll_health_once`, where a node's status is computed, detect an offline→online transition (`h is not None and not prev.get("online")`) and push the current effective trust config to that node. The aggregator needs a handle to the controller `db` (or a `get_fleet_trust`/`effective_fleet_ips` callable) — pass it into `FleetAggregator.__init__` where it is constructed in `controller/app.py` (alongside `_enabled_node_provider`). Add, inside the transition branch:

```python
# A node that just (re)joined may have restarted with an empty whitelist —
# re-push the fleet trust config so it re-enforces immediately.
if self._fleet_trust_provider is not None:
    try:
        t = self._fleet_trust_provider()   # {"ips": [...], "drop_untrusted": bool} (already effective)
        await self._client_for(node).set_trust_whitelist(t["ips"], t["drop_untrusted"])
    except Exception as e:
        logger.debug("trust re-push to rejoining node %s failed: %s", nid, e)
```

Wire `_fleet_trust_provider` through `__init__` (default `None`, so existing construction/tests are unaffected) and set it in `controller/app.py` to a lambda returning `{"ips": db.effective_fleet_ips(), "drop_untrusted": db.get_fleet_trust()["drop_untrusted"]}`.

- [ ] **Step 2: Test** — append a focused test to `tests/test_controller.py` (or test_fleet_discovery.py): construct a `FleetAggregator` with a stub `_fleet_trust_provider` + a node that flips offline→online, monkeypatch `NodeClient.set_trust_whitelist`, run one `_poll_health_once` (mock `health()` to succeed), and assert the push happened once on the transition (and not again while it stays online). Match the file's existing aggregator test setup.

- [ ] **Step 3: Run → passes.** Run the focused test, then `python -m pytest tests/test_controller.py tests/test_fleet_discovery.py -q`.

- [ ] **Step 4: Commit**

```bash
git add gencall/controller/aggregator.py gencall/controller/app.py tests/test_controller.py
git commit -m "feat(trust): re-push fleet whitelist to a worker on rejoin"
```

---

## Task 6: Remove the install-time whitelist prompt

**Files:**
- Modify: `deploy/install.sh`
- Modify: `deploy/install-ubuntu.sh`

- [ ] **Step 1: Edit `deploy/install.sh`**

Remove the MADA-whitelist prompt block (≈ lines 95–115): the `MADA="${MADA_IPS:-}"` block, the `read -rp ... Enter MADA IP(s)` prompt, the `set_cfg trust whitelist "$MADA"` call, and the "left empty — will FLAG" warning. **Keep** the `[sip] min/max_rtp_port` settings and the `set_cfg` helper (still used for RTP). Replace the removed block with a one-line note:

```bash
# Inbound trust whitelist is no longer set here — configure it from the
# controller console (Configuration → Inbound Trust), which pushes it to every
# worker at runtime. The HOST FIREWALL remains the real boundary (see docs).
```

- [ ] **Step 2: Edit `deploy/install-ubuntu.sh`**

Remove the equivalent block (≈ lines 152–157: the `MADA="${MADA_IPS:-}"` + `read -rp ... inbound whitelist` + `set_cfg trust whitelist`). Keep the other `set_cfg` calls (sipp command, RTP ports, web serve_console). Add the same one-line note.

- [ ] **Step 3: Verify**

Run:
```
grep -n "trust whitelist\|MADA_IPS\|Enter MADA\|inbound whitelist" deploy/install.sh deploy/install-ubuntu.sh
bash -n deploy/install.sh && bash -n deploy/install-ubuntu.sh && echo "syntax ok"
```
Expected: the grep shows no remaining prompt/`set_cfg trust whitelist` (only the new comment, if it contains "whitelist"); both scripts pass `bash -n` (syntax check).

- [ ] **Step 4: Commit**

```bash
git add deploy/install.sh deploy/install-ubuntu.sh
git commit -m "feat(trust): drop install-time whitelist prompt (now controller-managed)"
```

---

## Task 7: Frontend API client + types

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/api.ts`

- [ ] **Step 1: Types** — in `types.ts`:

```typescript
export interface FleetTrust {
  enabled: boolean;
  ips: string[];
  drop_untrusted: boolean;
}

export interface FleetTrustResult {
  enabled: boolean;
  ips: string[];
  drop_untrusted: boolean;
  pushed?: number;
  results?: { address: string; ok: boolean; error: string | null }[];
}
```

- [ ] **Step 2: API client** — in `api.ts` (add `FleetTrust`/`FleetTrustResult` to the type-import block), add to the `api` object:

```typescript
  getFleetTrust: () => request<FleetTrust>("/api/fleet/config/trust"),
  setFleetTrust: (body: FleetTrust) =>
    request<FleetTrustResult>("/api/fleet/config/trust", { method: "POST", body }),
```

- [ ] **Step 3: Verify** `cd frontend && npm run typecheck` (exit 0).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/lib/api.ts
git commit -m "feat(trust): frontend types + fleet trust API client"
```

---

## Task 8: Frontend — Inbound Trust panel on the Config page

**Files:**
- Modify: `frontend/src/pages/Config.tsx`

- [ ] **Step 1: Implement the panel** — add to `Config.tsx`, matching its existing `Panel`/`Field`/`Button`/`useToast`/`useAsync` usage:

```tsx
  const trust = useAsync(() => api.getFleetTrust(), []);
  const [trustEnabled, setTrustEnabled] = useState(false);
  const [trustIps, setTrustIps] = useState("");
  const [trustDrop, setTrustDrop] = useState(false);
  const [trustBusy, setTrustBusy] = useState(false);

  useEffect(() => {
    if (trust.data) {
      setTrustEnabled(trust.data.enabled);
      setTrustIps(trust.data.ips.join("\n"));
      setTrustDrop(trust.data.drop_untrusted);
    }
  }, [trust.data]);

  const applyTrust = async () => {
    const ips = trustIps.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    setTrustBusy(true);
    try {
      const res = await api.setFleetTrust({ enabled: trustEnabled, ips, drop_untrusted: trustDrop });
      const ok = (res.results ?? []).filter((r) => r.ok).length;
      const total = (res.results ?? []).length;
      toast.ok(`Trust applied · pushed to ${ok}/${total} worker(s)`);
      (res.results ?? []).filter((r) => !r.ok).forEach((r) => toast.error(`${r.address}: ${r.error}`));
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setTrustBusy(false);
    }
  };
```

JSX (inside the page's panel stack):

```tsx
      <Panel title="Inbound Trust Whitelist (fleet-wide)">
        <p style={{ color: "var(--text-muted)", fontSize: "var(--fs-sm)", marginTop: 0 }}>
          Allowed inbound SIP source IPs/CIDRs, pushed to every worker. Empty or disabled =
          allow-all (calls still recorded, just flagged). The host firewall remains the real boundary.
        </p>
        <label style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: "var(--space-3)" }}>
          <input type="checkbox" checked={trustEnabled} onChange={(e) => setTrustEnabled(e.target.checked)} />
          <span>Enforce whitelist (off = allow-all, keeps the list below)</span>
        </label>
        <Field label="Allowed IPs / CIDRs" hint="One per line or space/comma separated.">
          <textarea rows={5} value={trustIps} onChange={(e) => setTrustIps(e.target.value)}
                    placeholder={"203.0.113.10\n203.0.113.0/24"} />
        </Field>
        <label style={{ display: "flex", gap: 10, alignItems: "center", margin: "var(--space-2) 0 var(--space-3)" }}>
          <input type="checkbox" checked={trustDrop} onChange={(e) => setTrustDrop(e.target.checked)} />
          <span>Drop (vs. flag) calls from outside the whitelist</span>
        </label>
        <Button variant="primary" onClick={applyTrust} disabled={trustBusy}>
          {trustBusy ? "Applying…" : "Apply to all workers"}
        </Button>
      </Panel>
```

Add `useEffect`/`useState` to the React import if not already present.

- [ ] **Step 2: Verify** `cd frontend && npm run typecheck` (exit 0).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/Config.tsx
git commit -m "feat(trust): Inbound Trust panel on the Config page"
```

---

## Self-Review

- **Spec coverage (spec §5):** remove install prompt → Task 6; worker runtime endpoint (hot-apply) → Tasks 1-2; controller persists + fans out → Tasks 3-4; re-push on join → Task 5; frontend panel → Tasks 7-8; firewall stays manual → not touched (out of scope, noted). ✓
- **Placeholder scan:** code is concrete. The controller-side tasks (3-5) say "match the file's existing X" for the controller DB class name, the enabled-node enumerator, and the `app`-vs-`router` symbol — because those exact names live in files not quoted verbatim here; the implementer must read `gencall/controller/models.py`, `routes.py`, `app.py`, `aggregator.py` and bind to the real symbols. These are precise integration instructions, not vague placeholders.
- **Type/contract consistency:** worker `POST /api/config/trust` body `{ips, drop_untrusted}` == `NodeClient.set_trust_whitelist(ips, drop_untrusted)` payload. Controller `{enabled, ips, drop_untrusted}` == `FleetTrust` frontend type. Worker enforces `effective` ips (empty when disabled) computed controller-side.
- **Empty = allow-all** preserved everywhere (worker `set_trust([])`, controller `effective_fleet_ips`).

## Open Items (carried)
- Spec §5.4: controller is source of truth; a worker restart loses the runtime value until the next push — the Task-5 rejoin re-push covers the reconnect case. A worker that restarts while still "online" to the controller (no transition) would not be re-pushed; acceptable for now, note for hardening.

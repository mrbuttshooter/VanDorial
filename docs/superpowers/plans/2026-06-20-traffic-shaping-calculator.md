# Diurnal Traffic Shaping + Calculator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Shape a loop campaign's attempts to follow a daily (diurnal) curve so it reads as organic traffic, and give operators a Calculator that turns a daily minutes target + ACD into the peak CPS + concurrency to provision.

**Architecture:** One pure module (`traffic_profile.py`) owns the curve + the sizing math, shared by a Calculator API/UI (Phase 1) and a runtime shaper thread (Phase 2). The shaper steps a campaign's SIPp rate each hour via **overlap relaunch** (start a new dialer at the new rate, then gracefully drain the old) so there's no hourly dip; ACD + concurrency cap stay constant and the curve repeats daily.

**Tech Stack:** Python 3.10+ / FastAPI / SQLAlchemy / pytest; React + TypeScript (Vite).

**Phasing:** Phase 1 (Tasks 1–4) is the Calculator — no engine changes, ships on its own. Phase 2 (Tasks 5–9) is the runtime shaper. Anchors verified 2026-06-20.

---

## File Structure
- **Create** `gencall/core/traffic_profile.py` — pure: `make_curve()` + `calculate()`. One responsibility (curve + math), no I/O/DB. Shared by API and shaper.
- **Modify** `gencall/api/loops.py` — `POST /api/loops/traffic-calc` (Phase 1); profile fields on `StartLoopRequest` (Phase 2).
- **Modify** `gencall/db/models.py` — profile columns on `LoopPreset` + `_ADDED_COLUMNS` (Phase 2).
- **Modify** `gencall/core/loop_engine.py` — accept/persist profile on `start_campaign`; shaper thread + overlap-relaunch (Phase 2).
- **Modify** `gencall/core/config.py` — `loops_shaper_enabled` toggle (Phase 2).
- **Modify** `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts` — calculator + profile types/clients.
- **Modify** `frontend/src/pages/Loops.tsx` — Calculator modal (Phase 1) + a profile section on the preset form (Phase 2).
- **Tests:** `tests/test_traffic_profile.py` (new), `tests/test_traffic_calc_api.py` (new), `tests/test_loop_shaper.py` (new).

---

# PHASE 1 — Calculator

## Task 1: `traffic_profile` module (curve + math)

**Files:** Create `gencall/core/traffic_profile.py`; Test `tests/test_traffic_profile.py`.

- [ ] **Step 1: Failing tests** — create `tests/test_traffic_profile.py`:

```python
from gencall.core import traffic_profile as tp


def test_make_curve_shape_defaults():
    w = tp.make_curve()
    assert len(w) == 24
    assert max(w) == 1.0                      # plateau peaks at 1.0
    assert w[2] == 0.25                        # 02:00 sits at night_floor
    assert w[12] == 1.0                        # midday on the plateau
    # monotonic ramp up across the morning
    assert w[6] <= w[7] <= w[8] <= w[9] == 1.0


def test_make_curve_tz_offset_rotates():
    base = tp.make_curve(tz_offset=0)
    rot = tp.make_curve(tz_offset=3)
    assert rot[h := 0] == base[3 % 24]
    assert rot[12] == base[15]


def test_calculate_minutes_to_peak_cps_and_concurrent():
    # 1,000,000 min/day, ACD 120s -> 500,000 answered calls/day (~=attempts)
    r = tp.calculate(target_minutes=1_000_000, acd_s=120, profile={})
    assert r["attempts_per_day"] == 500_000
    # average cps = 500000/86400 ~= 5.79; peak is higher (diurnal concentration)
    assert abs(r["avg_cps"] - 500_000 / 86400) < 0.01
    assert r["peak_cps"] > r["avg_cps"]
    # peak concurrent = ceil(peak_cps * acd * 1.2)
    import math
    assert r["peak_concurrent"] == math.ceil(r["peak_cps"] * 120 * 1.2)
    # per-hour attempts sum back to the daily total (within rounding)
    assert abs(sum(h["attempts"] for h in r["per_hour"]) - 500_000) <= 24


def test_calculate_caps_warn_and_suggest_nodes():
    r = tp.calculate(target_minutes=5_000_000, acd_s=60, profile={},
                     max_cps=500, max_channels=1000)
    assert r["warnings"]                         # peak exceeds caps
    assert r["nodes_needed"] >= 2


def test_calculate_rejects_bad_acd():
    import pytest
    with pytest.raises(ValueError):
        tp.calculate(target_minutes=1000, acd_s=0, profile={})
```

- [ ] **Step 2: Run → fails** (`ModuleNotFoundError`). `python -m pytest tests/test_traffic_profile.py -v`

- [ ] **Step 3: Implement** — create `gencall/core/traffic_profile.py`:

```python
"""Diurnal traffic profile: a 24h attempt-weight curve + the sizing math that
turns a daily minutes target + ACD into per-hour CPS, peak CPS and peak
concurrency. Pure (no I/O / DB) so the Calculator API and the runtime shaper
compute identically."""
import math

PRESETS = ("diurnal",)

_CURVE_DEFAULTS = dict(
    night_floor=0.25, ramp_up_start=6, plateau_start=9,
    plateau_end=18, ramp_down_end=22, tz_offset=0,
)


def make_curve(preset="diurnal", *, night_floor=0.25, ramp_up_start=6,
               plateau_start=9, plateau_end=18, ramp_down_end=22,
               tz_offset=0):
    """Return 24 relative attempt weights (peak 1.0, night = night_floor).

    Trapezoid: night_floor overnight -> linear ramp up to 1.0 -> plateau ->
    linear ramp down to night_floor. ``tz_offset`` rotates the array so a box at
    hour ``t`` uses the destination market's local-hour weight: w[t] =
    base[(t + tz_offset) % 24]."""
    nf = max(0.0, min(1.0, float(night_floor)))
    rus, ps = int(ramp_up_start), int(plateau_start)
    pe, rde = int(plateau_end), int(ramp_down_end)
    base = []
    for h in range(24):
        if h < rus or h >= rde:
            v = nf
        elif rus <= h < ps:
            v = nf + (1.0 - nf) * (h - rus) / max(1, ps - rus)
        elif ps <= h <= pe:
            v = 1.0
        else:  # pe < h < rde
            v = 1.0 - (1.0 - nf) * (h - pe) / max(1, rde - pe)
        base.append(round(v, 4))
    off = int(tz_offset) % 24
    return [base[(h + off) % 24] for h in range(24)] if off else base


def calculate(target_minutes, acd_s, profile=None, *,
              max_cps=None, max_channels=None):
    """Size a diurnal campaign. ``profile`` is the make_curve kwargs (preset +
    knobs). Returns per-hour CPS, peak/avg CPS, peak concurrency, and (when caps
    are given) warnings + nodes_needed. No ASR: assumes ~100% answer (the loop
    UAS auto-answers), so attempts == answered for sizing."""
    acd_s = float(acd_s)
    if acd_s <= 0:
        raise ValueError("acd_s must be > 0")
    if float(target_minutes) < 0:
        raise ValueError("target_minutes must be >= 0")
    curve = make_curve(**(profile or {}))
    total = sum(curve) or 1.0
    attempts_per_day = float(target_minutes) * 60.0 / acd_s     # ~100% answer
    per_hour = []
    for h in range(24):
        attempts = attempts_per_day * curve[h] / total
        per_hour.append({"hour": h, "weight": curve[h],
                         "cps": round(attempts / 3600.0, 3),
                         "attempts": int(round(attempts))})
    peak_cps = max((x["cps"] for x in per_hour), default=0.0)
    avg_cps = attempts_per_day / 86400.0
    peak_concurrent = math.ceil(peak_cps * acd_s * 1.2)
    warnings, nodes_needed = [], 1
    if max_cps and peak_cps > max_cps:
        n = math.ceil(peak_cps / max_cps)
        nodes_needed = max(nodes_needed, n)
        warnings.append(f"peak {peak_cps} cps exceeds the {max_cps} cps cap — "
                        f"split across {n} nodes")
    if max_channels and peak_concurrent > max_channels:
        n = math.ceil(peak_concurrent / max_channels)
        nodes_needed = max(nodes_needed, n)
        warnings.append(f"peak {peak_concurrent} concurrent exceeds the "
                        f"{max_channels} channel cap — split across {n} nodes")
    return {
        "per_hour": per_hour,
        "peak_cps": round(peak_cps, 3),
        "avg_cps": round(avg_cps, 3),
        "peak_concurrent": peak_concurrent,
        "attempts_per_day": int(round(attempts_per_day)),
        "warnings": warnings,
        "nodes_needed": nodes_needed,
    }
```

- [ ] **Step 4: Run → passes.** `python -m pytest tests/test_traffic_profile.py -v`
- [ ] **Step 5: Commit**
```bash
git add gencall/core/traffic_profile.py tests/test_traffic_profile.py
git commit -m "feat(shaper): traffic_profile curve + sizing math"
```

---

## Task 2: `POST /api/loops/traffic-calc`

**Files:** Modify `gencall/api/loops.py`; Test `tests/test_traffic_calc_api.py`.

- [ ] **Step 1: Failing test** — create `tests/test_traffic_calc_api.py` (TestClient over the loops router, auth overridden, like `tests/test_trust_config.py`):

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gencall.api import loops as loops_mod
from gencall.api.routes import require_api_key


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(loops_mod.router)
    app.dependency_overrides[require_api_key] = lambda: None
    return TestClient(app)


def test_traffic_calc_returns_schedule(client):
    r = client.post("/api/loops/traffic-calc",
                    json={"target_minutes": 1_000_000, "acd_s": 120, "profile": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["peak_cps"] > body["avg_cps"] > 0
    assert len(body["per_hour"]) == 24
    assert "peak_concurrent" in body


def test_traffic_calc_validates(client):
    assert client.post("/api/loops/traffic-calc",
                       json={"target_minutes": 1000, "acd_s": 0}).status_code == 422
```

- [ ] **Step 2: Run → fails** (404).
- [ ] **Step 3: Implement** — in `gencall/api/loops.py`, add a model + endpoint (reuse the file's `BaseModel`, `Field`, `Depends`, `require_api_key`, `Config`):

```python
class TrafficCalcProfile(BaseModel):
    preset: str = "diurnal"
    night_floor: float = 0.25
    ramp_up_start: int = 6
    plateau_start: int = 9
    plateau_end: int = 18
    ramp_down_end: int = 22
    tz_offset: int = 0


class TrafficCalcRequest(BaseModel):
    target_minutes: int = Field(ge=0)
    acd_s: float = Field(gt=0)
    profile: TrafficCalcProfile = TrafficCalcProfile()


@router.post("/api/loops/traffic-calc", dependencies=[Depends(require_api_key)])
def traffic_calc(req: TrafficCalcRequest):
    """Size a diurnal campaign from a daily minutes target + ACD."""
    from gencall.core import traffic_profile
    cfg = Config()
    try:
        return traffic_profile.calculate(
            req.target_minutes, req.acd_s, req.profile.dict(exclude={"preset"}),
            max_cps=cfg.loops_max_rate_cps, max_channels=cfg.loops_max_channels)
    except ValueError as e:
        raise HTTPException(422, str(e))
```

(`profile.dict(exclude={"preset"})` passes only the curve knobs as make_curve kwargs; `preset` is accepted for forward-compat but the only preset today is the trapezoid.)

- [ ] **Step 4: Run → passes.** `python -m pytest tests/test_traffic_calc_api.py -v`
- [ ] **Step 5: Commit**
```bash
git add gencall/api/loops.py tests/test_traffic_calc_api.py
git commit -m "feat(shaper): POST /api/loops/traffic-calc"
```

---

## Task 3: Frontend types + API client

**Files:** Modify `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`.

- [ ] **Step 1: Types** — in `types.ts`:
```typescript
export interface TrafficProfile {
  preset: string;
  night_floor: number;
  ramp_up_start: number;
  plateau_start: number;
  plateau_end: number;
  ramp_down_end: number;
  tz_offset: number;
}

export interface TrafficCalcResult {
  per_hour: { hour: number; weight: number; cps: number; attempts: number }[];
  peak_cps: number;
  avg_cps: number;
  peak_concurrent: number;
  attempts_per_day: number;
  warnings: string[];
  nodes_needed: number;
}
```

- [ ] **Step 2: Client** — add to the `api` object in `api.ts` (add the two types to its import block):
```typescript
  trafficCalc: (body: { target_minutes: number; acd_s: number; profile: Partial<TrafficProfile> }) =>
    request<TrafficCalcResult>("/api/loops/traffic-calc", { method: "POST", body }),
```
- [ ] **Step 3: Verify** `cd frontend && npm run typecheck` → exit 0.
- [ ] **Step 4: Commit**
```bash
git add frontend/src/lib/types.ts frontend/src/lib/api.ts
git commit -m "feat(shaper): frontend traffic-calc types + client"
```

---

## Task 4: Calculator modal on the Loops page

**Files:** Modify `frontend/src/pages/Loops.tsx`.

- [ ] **Step 1: Implement** — add a **"Calculator"** button to the Loops page toolbar and a modal (match the page's existing `Modal`/`Field`/`FieldRow`/`Button`/`useToast` patterns). State + handler:

```tsx
  const [showCalc, setShowCalc] = useState(false);
  const [calc, setCalc] = useState({ target_minutes: 1000000, acd_s: 120, night_floor: 0.25 });
  const [calcRes, setCalcRes] = useState<TrafficCalcResult | null>(null);
  const [calcBusy, setCalcBusy] = useState(false);

  const runCalc = async () => {
    setCalcBusy(true);
    try {
      const res = await api.trafficCalc({
        target_minutes: Number(calc.target_minutes), acd_s: Number(calc.acd_s),
        profile: { night_floor: Number(calc.night_floor) },
      });
      setCalcRes(res);
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setCalcBusy(false);
    }
  };
```

Modal body: number inputs for **Daily minutes target**, **ACD (s)**, **Night floor (0–1)**; a **Calculate** button; and when `calcRes`, show **Peak CPS**, **Avg CPS**, **Peak concurrent**, a simple 24-bar inline sparkline of `per_hour[].cps`, and any `warnings`. Include an **"Apply to new preset"** button that opens the preset form pre-filled with `rate = calcRes.peak_cps`, `max_concurrent = calcRes.peak_concurrent` (and, once Phase 2 lands, `profile_enabled = true` + the knobs + `target_minutes`). For Phase 1, Apply fills `rate`/`max_concurrent` into `PRESET_BLANK` and opens the preset modal.

Sparkline (inline, no chart lib):
```tsx
<div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 60 }}>
  {calcRes.per_hour.map((h) => (
    <div key={h.hour} title={`${h.hour}:00 — ${h.cps} cps`}
         style={{ flex: 1, height: `${(h.cps / calcRes.peak_cps) * 100}%`,
                  background: "var(--signal, #4ade80)" }} />
  ))}
</div>
```

- [ ] **Step 2: Verify** `cd frontend && npm run typecheck` → exit 0.
- [ ] **Step 3: Commit**
```bash
git add frontend/src/pages/Loops.tsx
git commit -m "feat(shaper): Calculator modal on the Loops page"
```

---

# PHASE 2 — Runtime shaper

## Task 5: Persist the profile on presets + campaigns

**Files:** Modify `gencall/db/models.py`, `gencall/api/loops.py` (StartLoopRequest), `gencall/core/loop_engine.py` (start_campaign passthrough + persist), `frontend/src/lib/types.ts` (LoopPresetRequest). Test `tests/test_loop_shaper.py`.

- [ ] **Step 1: Failing test** — create `tests/test_loop_shaper.py` with a model round-trip:
```python
from gencall.db.models import Database, LoopPreset


def test_loop_preset_profile_columns_roundtrip(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'p.db'}")
    db.create_tables()
    s = db.get_session()
    try:
        s.add(LoopPreset(name="diurnal-1", profile_enabled=True,
                         profile_preset="diurnal", night_floor=0.3,
                         ramp_up_start=6, plateau_start=9, plateau_end=18,
                         ramp_down_end=22, tz_offset=3, target_minutes=1000000))
        s.commit()
        row = s.query(LoopPreset).one()
        d = row.to_dict()
        assert d["profile_enabled"] is True and d["night_floor"] == 0.3
        assert d["tz_offset"] == 3 and d["target_minutes"] == 1000000
    finally:
        s.close()
```

- [ ] **Step 2: Run → fails** (`TypeError: 'profile_enabled' is an invalid keyword`).
- [ ] **Step 3: Implement** — in `gencall/db/models.py`:
  - Add columns to `class LoopPreset` (after `rtp_loop`): `profile_enabled = Column(Boolean, default=False)`, `profile_preset = Column(String(32), default="diurnal")`, `night_floor = Column(Float, default=0.25)`, `ramp_up_start = Column(Integer, default=6)`, `plateau_start = Column(Integer, default=9)`, `plateau_end = Column(Integer, default=18)`, `ramp_down_end = Column(Integer, default=22)`, `tz_offset = Column(Integer, default=0)`. (`target_minutes` already exists.)
  - Add each to `LoopPreset.to_dict()` (booleans via `bool(...)`).
  - Register them in `_ADDED_COLUMNS["loop_presets"]` (idempotent ALTER for existing DBs), e.g. `("profile_enabled", "BOOLEAN DEFAULT 0")`, `("profile_preset", "VARCHAR(32) DEFAULT 'diurnal'")`, `("night_floor", "FLOAT DEFAULT 0.25")`, the four hour ints `"INTEGER DEFAULT <n>"`, `("tz_offset", "INTEGER DEFAULT 0")`.
  - Mirror the same eight columns on the `loop_campaigns` table model/record + its `_ADDED_COLUMNS` entry so a running campaign carries its profile.
  - In `gencall/api/loops.py` `StartLoopRequest` and `LoopPresetRequest` (and the create-preset handler), add the eight profile fields with the same defaults; thread them through to `start_campaign`.
  - In `gencall/core/loop_engine.py` `start_campaign`, accept the eight profile kwargs, store them in the `campaign` dict, and `_persist_campaign` them. (No behavior change yet — Task 7 reads them.)
  - In `frontend/src/lib/types.ts`, add the eight fields to `LoopPresetRequest`.

- [ ] **Step 4: Run → passes.** `python -m pytest tests/test_loop_shaper.py -v`; then `cd frontend && npm run typecheck`.
- [ ] **Step 5: Commit**
```bash
git add gencall/db/models.py gencall/api/loops.py gencall/core/loop_engine.py frontend/src/lib/types.ts tests/test_loop_shaper.py
git commit -m "feat(shaper): persist diurnal profile on presets + campaigns"
```

---

## Task 6: Overlap-relaunch helper (step a campaign's rate, no dip)

**Files:** Modify `gencall/core/loop_engine.py`; Test `tests/test_loop_shaper.py`.

- [ ] **Step 1: Failing test** — append to `tests/test_loop_shaper.py`. Use the existing `stub_sipp` fixture (conftest) + a real `LoopEngine` to start a campaign, then step its rate and assert a new instance replaces the old at the new rate:
```python
def test_step_campaign_rate_overlap_relaunch(stub_sipp):
    from gencall.core.loop_engine import LoopEngine
    eng = LoopEngine(config=stub_sipp.config)
    c = eng.start_campaign(dest_host="203.0.113.10", rate=2.0, max_concurrent=50,
                           duration_s=10, local_ip="")
    cid = c["id"]
    old_iid = eng._campaigns[cid]["instance_id"]
    eng.step_campaign_rate(cid, 5.0)
    new_iid = eng._campaigns[cid]["instance_id"]
    assert new_iid != old_iid
    inst = eng.engine.get_instance(new_iid)
    assert inst is not None and inst.call_rate == 5.0
    assert eng._campaigns[cid]["rate"] == 5.0
    # old instance is stopped/removed
    assert eng.engine.get_instance(old_iid) is None or \
        eng.engine.get_instance(old_iid).state.value in ("stopped", "stopping", "error")
    eng.stop_campaign(cid)
```

- [ ] **Step 2: Run → fails** (`AttributeError: 'LoopEngine' object has no attribute 'step_campaign_rate'`).
- [ ] **Step 3: Implement** — add to `LoopEngine` (mirror how `start_campaign` builds a `SIPpInstance` + `engine.start_instance` + `_register_logs`, and how `_monitor_loop` uses `engine.remove_instance`/`stop_instance`):

```python
def step_campaign_rate(self, campaign_id: str, new_rate: float) -> bool:
    """Change a running campaign's attempt rate with NO traffic dip: start a
    fresh UAC at ``new_rate``, then gracefully drain (SIGUSR1) the old one. ACD,
    concurrency cap, scenario, dest and source IP are carried over unchanged.
    Returns False if the campaign isn't running or new_rate is invalid/unchanged."""
    with self._lock:
        campaign = self._campaigns.get(campaign_id)
        if campaign is None or campaign.get("status") != "running":
            return False
        new_rate = float(new_rate)
        if new_rate <= 0 or new_rate > self.config.loops_max_rate_cps:
            return False
        if abs(new_rate - float(campaign.get("rate", 0))) < 1e-9:
            return False
        old_iid = campaign["instance_id"]
        old = self.engine.get_instance(old_iid)
        if old is None:
            return False
        # Build the replacement UAC from the old instance's settings + new rate.
        new_iid = f"uac-{campaign_id}-{int(new_rate * 1000)}-{self._step_seq}"
        self._step_seq += 1
        new = SIPpInstance(
            id=new_iid,
            scenario_file=old.scenario_file,
            remote_host=old.remote_host,
            remote_port=old.remote_port,
            local_port=0,                 # OS-assigned ephemeral (distinct from old)
            local_ip=old.local_ip,
            mode=SIPpMode.UAC,
            transport=old.transport,
            call_rate=new_rate,
            max_calls=old.max_calls,
            call_limit=old.call_limit,
            duration=old.duration,
            csv_file=old.csv_file,
            campaign_id=campaign_id,
        )
        if not self.engine.start_instance(new):
            logger.warning("shaper: replacement UAC failed for %s (%s); keeping old",
                           campaign_id, new.error_message)
            return False
        self._register_logs(new, campaign_id=campaign_id)
        campaign["instance_id"] = new_iid
        campaign["rate"] = new_rate
        self._persist_campaign(campaign)
    # Drain the old OUTSIDE the lock (stop_instance signals + waits): the new UAC
    # is already placing calls, so in-flight old calls finishing causes no dip.
    try:
        self.engine.stop_instance(old_iid)
        self.engine.remove_instance(old_iid)
    except Exception as e:
        logger.warning("shaper: draining old UAC %s failed: %s", old_iid, e)
    return True
```

Add `self._step_seq = 0` to `LoopEngine.__init__`.

> **Verify during implementation:** two UACs on the same `local_ip` with `-p 0` must get **distinct** OS-assigned source ports (they already get distinct `-mp` media ports from `_alloc_media_port`). The `stub_sipp` test confirms the lifecycle; on a real box confirm no "address already in use". If `-p 0` collides, allocate an explicit free SIP port for the replacement instead of 0.

- [ ] **Step 4: Run → passes.** `python -m pytest tests/test_loop_shaper.py -v`
- [ ] **Step 5: Commit**
```bash
git add gencall/core/loop_engine.py tests/test_loop_shaper.py
git commit -m "feat(shaper): overlap-relaunch step_campaign_rate (no dip)"
```

---

## Task 7: Shaper thread (hourly step along the curve)

**Files:** Modify `gencall/core/loop_engine.py`, `gencall/core/config.py`; Test `tests/test_loop_shaper.py`.

- [ ] **Step 1: Failing test** — append to `tests/test_loop_shaper.py`: with an injected hour, the shaper computes the right step rate for a profiled campaign:
```python
def test_shaper_computes_step_rate_for_hour(stub_sipp):
    from gencall.core.loop_engine import LoopEngine
    from gencall.core import traffic_profile
    eng = LoopEngine(config=stub_sipp.config)
    c = eng.start_campaign(dest_host="203.0.113.10", rate=1.0, max_concurrent=200,
                           duration_s=120, local_ip="",
                           profile_enabled=True, target_minutes=1_000_000,
                           night_floor=0.25)
    cid = c["id"]
    expected = traffic_profile.calculate(
        1_000_000, 120, {"night_floor": 0.25})["per_hour"][14]["cps"]
    rate14 = eng._shaper_target_rate(eng._campaigns[cid], hour=14)
    assert abs(rate14 - expected) < 1e-6
    eng.stop_campaign(cid)
```

- [ ] **Step 2: Run → fails** (`AttributeError: '_shaper_target_rate'`).
- [ ] **Step 3: Implement** —
  - `config.py`: add `loops_shaper_enabled` (`return self.getbool("loops", "shaper_enabled", True)`).
  - `loop_engine.py`: add a helper + a thread mirroring `_ensure_monitor`/`_monitor_loop`/`stop_monitor`:

```python
def _shaper_target_rate(self, campaign: dict, hour: int) -> float:
    """Per-hour attempt rate for a profiled campaign, clamped to the cap."""
    from gencall.core import traffic_profile
    prof = {k: campaign.get(k) for k in (
        "night_floor", "ramp_up_start", "plateau_start",
        "plateau_end", "ramp_down_end", "tz_offset") if campaign.get(k) is not None}
    acd = int(campaign.get("duration_s") or 0) or 1
    res = traffic_profile.calculate(int(campaign.get("target_minutes") or 0), acd, prof)
    cps = res["per_hour"][hour % 24]["cps"]
    return min(max(cps, 0.0), self.config.loops_max_rate_cps)

def _ensure_shaper(self):
    if not self.config.loops_shaper_enabled:
        return
    if self._shaper_thread is not None and self._shaper_thread.is_alive():
        return
    self._shaper_stop.clear()
    self._shaper_thread = threading.Thread(
        target=self._shaper_loop, daemon=True, name="loop-shaper")
    self._shaper_thread.start()

def _shaper_loop(self):
    """Each wake, for every running profiled campaign, step its rate to the
    current hour's curve value (overlap relaunch). Idles between wakes."""
    import time as _time
    while not self._shaper_stop.is_set():
        try:
            hour = _time.localtime().tm_hour
            for cid, campaign in list(self._campaigns.items()):
                if campaign.get("status") != "running" or not campaign.get("profile_enabled"):
                    continue
                target = self._shaper_target_rate(campaign, hour)
                if target > 0 and abs(target - float(campaign.get("rate", 0))) > 1e-3:
                    self.step_campaign_rate(cid, target)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("shaper pass failed: %s", e)
        self._shaper_stop.wait(SHAPER_INTERVAL_S)

def stop_shaper(self, timeout=5.0):
    self._shaper_stop.set()
    if self._shaper_thread is not None:
        self._shaper_thread.join(timeout=timeout)
        self._shaper_thread = None
```

  - Add module const `SHAPER_INTERVAL_S = 60.0` (checks each minute; steps only on an hour change since the rate then differs). Add `self._shaper_thread = None` and `self._shaper_stop = threading.Event()` to `__init__`. Call `self._ensure_shaper()` at the end of `start_campaign` (when `profile_enabled`), and `self.stop_shaper()` wherever `stop_monitor()` is called (engine shutdown). At campaign start, also set the initial rate to the current hour's value (call `step_campaign_rate` once, or start the UAC already at `_shaper_target_rate(campaign, now_hour)`).

- [ ] **Step 4: Run → passes.** `python -m pytest tests/test_loop_shaper.py -v`; then full suite `python -m pytest -q` (green except the 2 known pre-existing E.164 cases — now fixed if Task ran after; otherwise note them).
- [ ] **Step 5: Commit**
```bash
git add gencall/core/loop_engine.py gencall/core/config.py tests/test_loop_shaper.py
git commit -m "feat(shaper): hourly shaper thread stepping rate along the curve"
```

---

## Task 8: Frontend — profile section on the preset form + Calculator "Apply"

**Files:** Modify `frontend/src/pages/Loops.tsx`.

- [ ] **Step 1: Implement** — extend `PRESET_BLANK` with the eight profile fields (defaults matching the backend). In the preset create/edit modal add a **"Traffic profile"** section: an **Enable trend** checkbox, **preset** (just "diurnal" for now), **Daily minutes target**, **Night floor**, and the four hour knobs (ramp_up_start/plateau_start/plateau_end/ramp_down_end), plus **tz offset**; show the same 24-bar sparkline (reuse from Task 4 via a small `<Sparkline cps={...}/>` helper, computed by calling `api.trafficCalc` on change or a local curve preview). Wire the Calculator modal's **"Apply to new preset"** to open this form pre-filled (`rate`=peak_cps, `max_concurrent`=peak_concurrent, `profile_enabled`=true, knobs + `target_minutes`).
- [ ] **Step 2: Verify** `cd frontend && npm run typecheck` → exit 0.
- [ ] **Step 3: Commit**
```bash
git add frontend/src/pages/Loops.tsx
git commit -m "feat(shaper): traffic-profile section on the preset form + Apply"
```

---

## Task 9: Docs note

**Files:** Modify `docs/deploy/loop-runner.md` (or the nearest loops doc).

- [ ] **Step 1** Add a short "Traffic shaping" note: profiled campaigns step their rate hourly (overlap relaunch, no dip); the curve repeats daily; sized via the Calculator from a daily minutes target + ACD; `[loops] shaper_enabled` toggles it. Commit:
```bash
git add docs/deploy/loop-runner.md
git commit -m "docs(shaper): document diurnal traffic shaping + calculator"
```

---

## Self-Review

- **Spec coverage:** curve model + knobs → Task 1 (§2); sizing math/no-ASR → Task 1 (§3); Calculator API → Task 2 (§4.1); Calculator UI + Apply → Tasks 4/8 (§4.2); profile persistence → Task 5 (§5.1); shaper thread → Task 7 (§5.2); overlap relaunch → Task 6 (§5.3); continuous daily repeat → Task 7 (curve indexed by current hour, no stop); caps/warnings → Tasks 1–2; tz_offset → Tasks 1/5/7. All §-points covered.
- **Placeholder scan:** Phase-1 code is complete. Task 5 and Task 8 enumerate exact columns/fields and the UI section but defer per-file line placement to the implementer (the models/Loops.tsx already have established patterns) — precise instructions, not vague TODOs. Task 6 carries an explicit verify-the-port-behavior note (a real risk, not a placeholder).
- **Type/contract consistency:** `make_curve` kwargs == the 6 profile knobs == `TrafficCalcProfile` == the 8 DB/preset fields (knobs + `profile_enabled`/`profile_preset`) == `LoopPresetRequest` additions. `calculate()` result keys == `TrafficCalcResult`. `step_campaign_rate`/`_shaper_target_rate`/`_ensure_shaper` names consistent across Tasks 6–7.
- **Risk flagged:** two-UAC-same-IP source-port behavior on overlap (Task 6 verify step) is the one thing to confirm on a real box; everything else is covered by stub tests.

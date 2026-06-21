# GenCall — Diurnal Traffic Shaping + Trend-Aware CPS/Concurrency Calculator

**Date:** 2026-06-20
**Status:** Approved design (pre-implementation)
**Scope:** One feature, **two phases**. Phase 1 (Calculator) ships independently; Phase 2 (Shaper) builds on the shared profile/math.

---

## 1. Background & Goals

Loop campaigns today run at a **flat rate and duration 24/7**. Constant, round-the-clock traffic (e.g. heavy attempts at 02:00) doesn't look like organic human traffic, so a carrier's technical/fraud controls flag or reject it even when the commercial deal is in place. Two wants:

1. **Diurnal traffic shaping** — make a campaign's **attempts (CPS)** follow a daily curve (low overnight, business-hours peak) like real traffic; since minutes = answered × ACD and ACD stays steady, the **minutes curve follows automatically** (both panels of the reference dashboard).
2. **A trend-aware Calculator** — operators don't know what CPS/concurrency to set for a minutes goal. Given a **daily minutes target + ACD**, compute the **peak CPS** and **peak concurrent** needed *for the chosen curve* (sizing for the peak, not a flat average), and apply them to the campaign.

### Key constraints (from code exploration)
- A running SIPp dialer uses a **fixed `-r` rate for its process lifetime** — no live rate knob is wired (`update_call_rate` only mutates memory). Shaping therefore **steps** the rate on a schedule by relaunching the dialer.
- `target_minutes` exists on the preset/campaign models but is **not enforced** at runtime today.
- Minutes are measured as `minutes_out_ms = Σ duration_ms of answered outbound calls`; `ACD = minutes_out_ms / answered_out`; `ASR = answered_out / calls_out` (loop_matcher).
- Caps to respect: `loops_max_rate_cps` (500), `loops_max_channels` (1000), `loops_max_answered` (1100).

---

## 2. Shared core — the Traffic Profile

A **daily 24-hour weight curve** `w[0..23]` (relative attempt level per hour), generated from a built-in **`diurnal` preset** plus knobs:

| knob | default | meaning |
|---|---|---|
| `night_floor` | 0.25 | overnight level relative to peak (0–1) |
| `ramp_up_start` | 6 | hour attempts start rising from the floor |
| `plateau_start` | 9 | hour the business-hours plateau (=1.0) begins |
| `plateau_end` | 18 | hour the plateau ends |
| `ramp_down_end` | 22 | hour attempts return to the floor |
| `tz_offset` | 0 | hours offset applied to the curve so "night" aligns with the **destination market's** local time, not the box's |

Curve generation (trapezoid): `night_floor` overnight → linear ramp `night_floor→1.0` over `[ramp_up_start, plateau_start]` → `1.0` over `[plateau_start, plateau_end]` → linear ramp `1.0→night_floor` over `[plateau_end, ramp_down_end]` → `night_floor`. `tz_offset` rotates the array.

Owned by one **pure** module `gencall/core/traffic_profile.py` (no I/O, no DB) so the Calculator and the Shaper compute **identically**.

---

## 3. The math (Calculator = Shaper core)

Inputs: **`target_minutes`** (per day), **`acd_s`** (seconds), and the curve `w[]`. **No ASR input** — we size assuming the loop **answers ~100%** (the UAS auto-answers every INVITE that routes back), so attempts ≈ answered for sizing.

```
answered_per_day = target_minutes / (acd_s / 60)      # = target_minutes * 60 / acd_s
attempts_per_day ≈ answered_per_day                    # ~100% answer assumption
attempts_hour[h] = attempts_per_day * w[h] / sum(w)
cps[h]           = attempts_hour[h] / 3600
peak_cps         = max(cps[h])
avg_cps          = attempts_per_day / 86400
peak_concurrent  = ceil(peak_cps * acd_s * 1.2)        # Erlangs + 20% setup/ring headroom
```

**Cap handling:** if `peak_cps > loops_max_rate_cps` or `peak_concurrent > loops_max_channels`, return a warning with `nodes_needed = ceil(peak / cap)` (ties into the existing node-group fan-out — split the campaign across N nodes).

**Honest caveat (surfaced in the UI):** if the switch rejects a meaningful share of calls (cause 3/47/…), real minutes land below target; the operator raises the target or rate to compensate. Answer-rate is switch-dependent, not guessed.

---

## 4. Phase 1 — Calculator

### 4.1 Backend
- `gencall/core/traffic_profile.py` — curve generation + `calculate(target_minutes, acd_s, profile)` returning `{per_hour:[{hour,cps,attempts}], peak_cps, avg_cps, peak_concurrent, warnings:[...]}`. Pure + fully unit-tested.
- `POST /api/loops/traffic-calc` (in `gencall/api/loops.py`, `require_api_key`) — body `{target_minutes, acd_s, profile:{preset, night_floor, ramp_up_start, plateau_start, plateau_end, ramp_down_end, tz_offset}}` → the calculate() result. Validates inputs; clamps caps; includes warnings.

### 4.2 Frontend
- A **"Calculator"** button on the Loops page (near the preset/run controls) → a modal:
  - Inputs: **Daily minutes target**, **ACD (s)**, curve **preset + knobs** (with sensible defaults), `tz_offset`.
  - Outputs: **peak CPS**, avg CPS, **peak concurrent**, a **24-hour sparkline** preview of the curve, and any cap warnings ("peak 1640 concurrent > 1000 cap → needs 2 nodes").
  - **"Apply to preset"**: fills the (open or selected) preset's `rate` = peak CPS and `max_concurrent` = peak concurrent, and enables the profile with the chosen knobs + target.
- `api.ts`: `trafficCalc(body)`; `types.ts`: `TrafficProfile`, `TrafficCalcResult`.

Phase 1 delivers immediate value ("what do we put") with **no engine changes**.

---

## 5. Phase 2 — Diurnal Shaper (runtime)

### 5.1 Profile persistence
Add to `LoopPreset` and the `loop_campaigns` record (via the existing `_ADDED_COLUMNS` idempotent-migration pattern):
`profile_enabled` (bool), `profile_preset` (str), the knob columns (`night_floor`, `ramp_up_start`, `plateau_start`, `plateau_end`, `ramp_down_end`, `tz_offset`), and `target_minutes` (already present, now meaningful). `StartLoopRequest`/`LoopPresetRequest` gain the matching fields.

### 5.2 Scheduler
A background **shaper thread** in `LoopEngine` (wakes each minute; acts on hour boundaries):
- For every running campaign with `profile_enabled`, compute `cps[current_hour]` from `(target_minutes, acd_s=duration_s, curve)` using `traffic_profile` (same math as the calculator).
- If the campaign's current dialer rate differs from `cps[hour]`, **step it** via **overlap relaunch** (§5.3).
- Concurrency cap (`-l`) and ACD (`-d`) stay **constant** at the calculated peak; only `-r` steps. The curve **repeats every day** (continuous run) until the campaign is stopped.
- Wakes on a `threading.Event` (no busy loop), mirroring the existing matcher/monitor threads.

### 5.3 Overlap relaunch (no hourly dip)
At a step: **start a new UAC** instance for the campaign at the new `-r` (unique ephemeral SIP port + media port, as the engine already allocates), wait until it's `RUNNING`, then send the **old** instance `SIGUSR1` (graceful drain — it stops placing new calls; in-flight calls finish, then it exits). This avoids the ~drain-time gap a stop-then-start would create (regular hourly notches would themselves look unnatural). The per-campaign step logic permits a transient second instance for the **same** `campaign_id` (the "one loop per IP" guard applies across distinct campaigns, not within a campaign's own transition). The campaign tracks its "current" instance id.

### 5.4 target_minutes
With the shaper, the daily curve is **sized** to hit ~`target_minutes`/day; the campaign runs continuously. (Optional, out of scope for v1: hard auto-stop when a one-day measured `minutes_out_ms` reaches target — the continuous model is the requested behavior.)

### 5.5 Frontend
- Preset/Run form: a **"Traffic profile"** section — enable trend, pick preset, edit knobs, set daily minutes target; a live 24h sparkline (reusing the calculator preview). The Calculator's "Apply" populates these.

---

## 6. Data Flow

- **Calculator:** UI inputs → `POST /api/loops/traffic-calc` → `traffic_profile.calculate()` → peak CPS/concurrent + per-hour + warnings → "Apply" writes them onto the preset.
- **Shaper:** campaign starts with a profile → shaper thread each hour computes `cps[hour]` (same module) → overlap-relaunch the UAC at the new rate → curve repeats daily.

---

## 7. Testing

**Phase 1 (pure + API):**
- `traffic_profile`: curve generation (floor/ramp/plateau shape; `tz_offset` rotation; weights normalized); `calculate()` math — `answered/day = target/(acd/60)`, per-hour distribution sums to attempts/day, `peak_cps`/`peak_concurrent` correct, cap warnings + `nodes_needed`; edge cases (acd=0 guard, target=0, flat curve).
- API: `POST /api/loops/traffic-calc` returns the schedule; validation (negative/zero inputs → 422); caps reflected.
- Frontend: typecheck; calculator modal computes + "Apply" fills rate/max_concurrent.

**Phase 2 (shaper):**
- Profile persistence round-trips (preset + campaign columns).
- Scheduler: with a fake clock/injected hour, the computed step rate matches `cps[hour]`; a rate change triggers exactly one overlap relaunch; no relaunch when rate is unchanged; thread starts/stops cleanly and is idle-safe.
- Overlap relaunch: new instance reaches RUNNING before the old gets SIGUSR1; the campaign's tracked instance id updates; the per-IP guard isn't tripped by the same-campaign transition.
- Full suite stays green.

---

## 8. Out of Scope
- Varying **ACD** by hour (only CPS/attempts is shaped; minutes follow). 
- Hard auto-stop at a measured minutes target (continuous repeating run is the requested model).
- Per-call duration from the CSV `[field2]` (pre-existing latent gap; not needed here).
- Seamless live rate change via SIPp remote control (rejected — fragile/version-dependent; overlap relaunch chosen).
- Wiring an answer-ratio (ASR) into sizing (operator compensates via target/rate per §3 caveat).

## 9. Open Items to Confirm in Planning
1. Profile columns added to `loop_presets` + `loop_campaigns` via `_ADDED_COLUMNS` (idempotent ALTER), matching the existing pattern.
2. Shaper timezone: `tz_offset` on the profile aligns the curve to the destination market; default 0 (box local). Confirm whether per-campaign offset suffices (yes for v1).
3. Overlap-relaunch transient-instance id scheme + ensuring `_alloc_media_port`/ephemeral SIP port give the second instance distinct ports (engine already does).
4. Step granularity fixed at hourly for v1 (finer is a config knob later).

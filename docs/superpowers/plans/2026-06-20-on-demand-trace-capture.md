# On-Demand Trace (pcap) Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** Per running loop, capture its packets with `tcpdump` ON THE WORKER, keep the file on the worker, and pull it to the controller only on explicit request. On-demand start/stop, never automatic.

**Architecture:** A new DB-free `CaptureManager` (`gencall/core/capture.py`) runs/track `tcpdump` processes, with a watchdog that auto-stops at a size or duration cap so a forgotten capture can't fill the disk. The BPF filter is built from the campaign's SIPp ports + destination switch IP. Worker per-campaign endpoints (in `gencall/api/loops.py`, reusing `_engine()`) manage captures; controller "fleet-capture" endpoints route by `box` (local → local manager; remote → proxy via `NodeClient`/`_worker_post`), and the download is **streamed** (never buffered) through the controller. Frontend adds capture controls + a captures list on the Loops page. Files live in `config.sipp_stats_dir`. Captures persist on the worker until explicitly deleted.

**Tech Stack:** Python/FastAPI (sync endpoints + StreamingResponse), `subprocess`/`tcpdump`, pytest; React/TS frontend. `tcpdump` is Linux-only — endpoints must fail clearly (not crash) when it's absent (e.g. on a Windows dev box), and tests use a cross-platform stub.

File:line anchors verified by grep on 2026-06-20.

---

## Task 1: Capture config accessors

**Files:** Modify `gencall/core/config.py`; Test `tests/test_capture.py` (new).

- [ ] **Step 1: Failing test** — create `tests/test_capture.py`:

```python
from gencall.core.config import Config


def test_capture_config_defaults(tmp_path, monkeypatch):
    cfg_path = tmp_path / "gencall.cfg"
    cfg_path.write_text("[sipp]\nstats_dir = %s\n" % (tmp_path / "stats"), encoding="utf-8")
    Config.reset()
    monkeypatch.setenv("GENCALL_CONFIG", str(cfg_path))
    c = Config(path=str(cfg_path))
    assert c.capture_command == "tcpdump"
    assert c.capture_max_seconds == 300
    assert c.capture_max_mb == 100
    assert c.capture_snaplen == 0
    # capture dir defaults to the sipp stats dir
    assert c.capture_dir == c.sipp_stats_dir
    Config.reset()
```

- [ ] **Step 2: Run → fails** (`AttributeError: ... 'capture_command'`).

Run: `python -m pytest tests/test_capture.py -k capture_config -v`

- [ ] **Step 3: Implement** — add to `Config` (near `sipp_stats_dir`):

```python
@property
def capture_command(self):
    """tcpdump binary used for on-demand pcap captures."""
    return self.get("capture", "command", "tcpdump")

@property
def capture_dir(self):
    """Where pcap captures are written (defaults to the sipp stats dir)."""
    return self.get("capture", "dir", "") or self.sipp_stats_dir

@property
def capture_max_seconds(self):
    """Auto-stop a capture after this many seconds (watchdog). 0 = no limit."""
    return self.getint("capture", "max_seconds", 300)

@property
def capture_max_mb(self):
    """Auto-stop a capture once its file exceeds this many MB. 0 = no limit."""
    return self.getint("capture", "max_mb", 100)

@property
def capture_snaplen(self):
    """tcpdump -s snaplen (0 = full packet)."""
    return self.getint("capture", "snaplen", 0)
```

(Use the same `getint` helper the other int properties use; if the file uses `getint`/`get`/`getbool` accessors, match them.)

- [ ] **Step 4: Run → passes.**

- [ ] **Step 5: Commit**

```bash
git add gencall/core/config.py tests/test_capture.py
git commit -m "feat(trace): capture config accessors"
```

---

## Task 2: `CaptureManager` + BPF filter builder

**Files:** Create `gencall/core/capture.py`; Test `tests/test_capture.py`; new stub `tests/stubs/fake_tcpdump.py`.

- [ ] **Step 1: Failing tests** — append to `tests/test_capture.py`:

```python
import os, sys, time
from gencall.core.capture import build_capture_filter, CaptureManager


def test_build_capture_filter_scopes_to_dest_and_ports():
    f = build_capture_filter(dest_host="203.0.113.10", dest_port=5060,
                             local_port=5071, media_port=10000, transport="udp")
    assert "host 203.0.113.10" in f
    assert "port 5060" in f
    assert "port 5071" in f
    # RTP base + RTCP/echo neighbours
    assert "portrange 10000-10002" in f


def test_capture_manager_start_list_stop_delete(tmp_path, monkeypatch):
    # Cross-platform fake tcpdump: writes the -w file then sleeps until killed.
    stub = os.path.join(os.path.dirname(__file__), "stubs", "fake_tcpdump.py")
    mgr = CaptureManager(command=f'"{sys.executable}" "{stub}"', capture_dir=str(tmp_path),
                         max_seconds=0, max_mb=0)
    cap = mgr.start(campaign_id="loop-abc", bpf="udp", iface="any")
    assert cap["running"] is True
    listed = mgr.list("loop-abc")
    assert len(listed) == 1 and listed[0]["id"] == cap["id"]
    # the output file exists
    assert os.path.isfile(mgr.path(cap["id"]))
    mgr.stop(cap["id"])
    time.sleep(0.2)
    assert mgr.list("loop-abc")[0]["running"] is False
    mgr.delete(cap["id"])
    assert mgr.list("loop-abc") == []
```

Create `tests/stubs/fake_tcpdump.py`:

```python
"""Cross-platform fake `tcpdump`: parse `-w <path>`, create the file, then sleep
until terminated. Lets CaptureManager tests run with no real tcpdump (Windows)."""
import sys, time

def main(argv):
    path = None
    for i, a in enumerate(argv):
        if a == "-w" and i + 1 < len(argv):
            path = argv[i + 1]
    if path:
        with open(path, "wb") as fh:
            fh.write(b"\xd4\xc3\xb2\xa1")  # pcap magic; enough for a real file
    try:
        while True:
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 2: Run → fails** (`ModuleNotFoundError: gencall.core.capture`).

Run: `python -m pytest tests/test_capture.py -v`

- [ ] **Step 3: Implement** — create `gencall/core/capture.py`:

```python
"""On-demand pcap capture: run/track tcpdump per loop, with a size/duration
watchdog. DB-free; the API layer resolves a campaign's ports and calls in here."""
import logging
import os
import shlex
import signal
import subprocess
import threading
import time
import uuid

logger = logging.getLogger("gencall.capture")

_HAS_SETSID = hasattr(os, "setsid") and hasattr(os, "killpg") and hasattr(os, "getpgid")


def build_capture_filter(dest_host, dest_port=5060, local_port=0,
                         media_port=0, transport="udp") -> str:
    """A BPF filter scoping the capture to one loop: signalling + RTP to/from the
    destination switch. tcpdump is run on 'any' iface, so we filter by host+ports."""
    proto = "tcp" if str(transport).lower().startswith("t") else "udp"
    parts = []
    if dest_host:
        parts.append(f"host {dest_host}")
    ports = []
    if dest_port:
        ports.append(f"{proto} port {dest_port}")
    if local_port:
        ports.append(f"{proto} port {local_port}")
    if media_port:
        # RTP (p), RTCP (p+1), and SIPp's -rtp_echo mirror (p+2).
        ports.append(f"udp portrange {media_port}-{media_port + 2}")
    expr = ""
    if parts:
        expr = parts[0]
    if ports:
        port_expr = "(" + " or ".join(ports) + ")"
        expr = f"{expr} and {port_expr}" if expr else port_expr
    return expr or proto


class _Capture:
    def __init__(self, cap_id, campaign_id, path, proc):
        self.id = cap_id
        self.campaign_id = campaign_id
        self.path = path
        self.proc = proc
        self.started_at = None  # epoch; set by caller via time (passed in to avoid Date in tests? use time.time here)
        self.stopped_at = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None


class CaptureManager:
    """Starts/stops/tracks tcpdump captures. One watchdog thread enforces the
    size/duration caps across all captures."""

    def __init__(self, command="tcpdump", capture_dir="/tmp",
                 max_seconds=300, max_mb=100, snaplen=0):
        self._command = command
        self._dir = capture_dir
        self._max_seconds = int(max_seconds)
        self._max_bytes = int(max_mb) * 1024 * 1024
        self._snaplen = int(snaplen)
        self._caps: dict[str, _Capture] = {}
        self._lock = threading.Lock()
        self._wd = None

    def start(self, campaign_id, bpf, iface="any") -> dict:
        os.makedirs(self._dir, exist_ok=True)
        cap_id = uuid.uuid4().hex[:12]
        path = os.path.join(self._dir, f"gencall_pcap_{campaign_id}_{cap_id}.pcap")
        cmd = shlex.split(self._command) + [
            "-i", iface, "-w", path, "-s", str(self._snaplen), "-U", bpf,
        ]
        popen_kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if _HAS_SETSID:
            popen_kwargs["preexec_fn"] = os.setsid
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError:
            raise RuntimeError(f"tcpdump not found ({self._command!r}); install it on the worker")
        time.sleep(0.3)
        if proc.poll() is not None:
            raise RuntimeError(
                f"tcpdump exited immediately (code {proc.returncode}); "
                "check it has CAP_NET_RAW / runs as root on this worker")
        cap = _Capture(cap_id, campaign_id, path, proc)
        cap.started_at = time.time()
        with self._lock:
            self._caps[cap_id] = cap
        self._ensure_watchdog()
        return self._info(cap)

    def stop(self, cap_id) -> dict:
        cap = self._get(cap_id)
        if cap.running():
            try:
                if _HAS_SETSID:
                    os.killpg(os.getpgid(cap.proc.pid), signal.SIGTERM)
                else:
                    cap.proc.terminate()
                cap.proc.wait(timeout=5)
            except Exception:
                try:
                    cap.proc.kill()
                except Exception:
                    pass
            cap.stopped_at = time.time()
        return self._info(cap)

    def list(self, campaign_id=None) -> list:
        with self._lock:
            caps = [c for c in self._caps.values()
                    if campaign_id is None or c.campaign_id == campaign_id]
        return [self._info(c) for c in caps]

    def path(self, cap_id) -> str:
        return self._get(cap_id).path

    def delete(self, cap_id) -> None:
        cap = self._get(cap_id)
        if cap.running():
            self.stop(cap_id)
        try:
            if os.path.isfile(cap.path):
                os.remove(cap.path)
        except OSError as e:
            logger.warning("could not delete capture %s: %s", cap.path, e)
        with self._lock:
            self._caps.pop(cap_id, None)

    # ── internals ──
    def _get(self, cap_id) -> "_Capture":
        with self._lock:
            cap = self._caps.get(cap_id)
        if cap is None:
            raise KeyError(cap_id)
        return cap

    def _info(self, cap) -> dict:
        size = os.path.getsize(cap.path) if os.path.isfile(cap.path) else 0
        return {"id": cap.id, "campaign_id": cap.campaign_id,
                "running": cap.running(), "size_bytes": size,
                "started_at": cap.started_at, "stopped_at": cap.stopped_at}

    def _ensure_watchdog(self):
        if (self._max_seconds <= 0 and self._max_bytes <= 0):
            return
        if self._wd is not None and self._wd.is_alive():
            return
        self._wd = threading.Thread(target=self._watch, daemon=True, name="capture-watchdog")
        self._wd.start()

    def _watch(self):
        while True:
            time.sleep(2.0)
            with self._lock:
                caps = list(self._caps.values())
            if not any(c.running() for c in caps):
                return  # idle; a new start() relaunches the watchdog
            now = time.time()
            for c in caps:
                if not c.running():
                    continue
                too_long = self._max_seconds > 0 and c.started_at and (now - c.started_at) > self._max_seconds
                too_big = self._max_bytes > 0 and os.path.isfile(c.path) and os.path.getsize(c.path) > self._max_bytes
                if too_long or too_big:
                    logger.info("auto-stopping capture %s (%s)", c.id,
                                "duration" if too_long else "size")
                    try:
                        self.stop(c.id)
                    except Exception:
                        pass
```

- [ ] **Step 4: Run → passes.** `python -m pytest tests/test_capture.py -v`

- [ ] **Step 5: Commit**

```bash
git add gencall/core/capture.py tests/test_capture.py tests/stubs/fake_tcpdump.py
git commit -m "feat(trace): CaptureManager + BPF filter builder"
```

---

## Task 3: Worker per-campaign capture endpoints + wiring

**Files:** Modify `gencall/api/loops.py`, `gencall/main.py`; Test `tests/test_capture_api.py` (new).

- [ ] **Step 1: Failing test** — create `tests/test_capture_api.py` modelled on `tests/test_trust_config.py`/`test_sale_zones.py`'s TestClient setup. Stub the manager so no tcpdump is needed:

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gencall.api import loops as loops_mod
from gencall.api.routes import require_api_key


class FakeManager:
    def __init__(self): self._n = 0; self.caps = {}
    def start(self, campaign_id, bpf, iface="any"):
        self._n += 1; cid = f"c{self._n}"
        self.caps[cid] = {"id": cid, "campaign_id": campaign_id, "running": True,
                          "size_bytes": 4, "started_at": 1.0, "stopped_at": None}
        return self.caps[cid]
    def stop(self, cid): self.caps[cid]["running"] = False; return self.caps[cid]
    def list(self, campaign_id=None):
        return [c for c in self.caps.values() if campaign_id in (None, c["campaign_id"])]
    def delete(self, cid): self.caps.pop(cid, None)
    def path(self, cid): return __file__  # any readable file for the download test


class FakeEngine:
    def get_campaign(self, cid):
        if cid != "loop-x":
            raise KeyError(cid)
        return {"id": cid, "dest_host": "203.0.113.10", "dest_port": 5060,
                "transport": "udp",
                "sipp": {"local_port": 5071, "media_port": 10000}}


@pytest.fixture
def client():
    loops_mod.loop_engine = FakeEngine()
    loops_mod.capture_manager = FakeManager()
    app = FastAPI()
    app.include_router(loops_mod.router)
    app.dependency_overrides[require_api_key] = lambda: None
    return TestClient(app)


def test_capture_lifecycle(client):
    r = client.post("/api/loops/loop-x/capture/start"); assert r.status_code == 200, r.text
    cid = r.json()["capture"]["id"]
    assert any(c["id"] == cid for c in client.get("/api/loops/loop-x/captures").json()["captures"])
    assert client.post(f"/api/loops/loop-x/capture/{cid}/stop").status_code == 200
    dl = client.get(f"/api/loops/loop-x/capture/{cid}/download")
    assert dl.status_code == 200 and len(dl.content) > 0
    assert client.delete(f"/api/loops/loop-x/capture/{cid}").status_code == 200


def test_capture_start_unknown_campaign_404(client):
    assert client.post("/api/loops/nope/capture/start").status_code == 404
```

- [ ] **Step 2: Run → fails** (404 routes absent).

- [ ] **Step 3: Implement** — in `gencall/api/loops.py`:

Add a module global near `call_parser`:
```python
capture_manager = None  # gencall.core.capture.CaptureManager, wired in main.py
```

Add a helper + the endpoints (after the loop endpoints):
```python
from fastapi.responses import StreamingResponse  # add to the fastapi.responses import

def _capture_mgr():
    if capture_manager is None:
        raise HTTPException(503, "capture not configured on this worker")
    return capture_manager


@router.post("/api/loops/{campaign_id}/capture/start", dependencies=[Depends(require_api_key)])
def capture_start(campaign_id: str):
    """Start a tcpdump capture for a running loop (filtered to its dest switch)."""
    from gencall.core.capture import build_capture_filter
    try:
        c = _engine().get_campaign(campaign_id)
    except KeyError:
        raise HTTPException(404, f"loop campaign '{campaign_id}' not found")
    sipp = c.get("sipp") or {}
    bpf = build_capture_filter(
        dest_host=c.get("dest_host", ""), dest_port=c.get("dest_port", 5060),
        local_port=(sipp.get("local_port") or 0), media_port=(sipp.get("media_port") or 0),
        transport=c.get("transport", "udp"))
    try:
        cap = _capture_mgr().start(campaign_id, bpf)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"status": "capturing", "capture": cap}


@router.post("/api/loops/{campaign_id}/capture/{capture_id}/stop", dependencies=[Depends(require_api_key)])
def capture_stop(campaign_id: str, capture_id: str):
    try:
        return {"status": "stopped", "capture": _capture_mgr().stop(capture_id)}
    except KeyError:
        raise HTTPException(404, f"capture '{capture_id}' not found")


@router.get("/api/loops/{campaign_id}/captures", dependencies=[Depends(require_api_key)])
def capture_list(campaign_id: str):
    return {"captures": _capture_mgr().list(campaign_id)}


@router.get("/api/loops/{campaign_id}/capture/{capture_id}/download", dependencies=[Depends(require_api_key)])
def capture_download(campaign_id: str, capture_id: str):
    import os
    try:
        path = _capture_mgr().path(capture_id)
    except KeyError:
        raise HTTPException(404, f"capture '{capture_id}' not found")
    if not os.path.isfile(path):
        raise HTTPException(404, "capture file not found")
    def _chunks():
        with open(path, "rb") as fh:
            while True:
                b = fh.read(65536)
                if not b:
                    break
                yield b
    return StreamingResponse(_chunks(), media_type="application/vnd.tcpdump.pcap",
                             headers={"Content-Disposition": f'attachment; filename="{capture_id}.pcap"'})


@router.delete("/api/loops/{campaign_id}/capture/{capture_id}", dependencies=[Depends(require_api_key)])
def capture_delete(campaign_id: str, capture_id: str):
    try:
        _capture_mgr().delete(capture_id)
    except KeyError:
        raise HTTPException(404, f"capture '{capture_id}' not found")
    return {"status": "deleted", "id": capture_id}
```

In `gencall/main.py`, after the LoopEngine/`loops_api` wiring (near where `loops_api.call_parser` is set), add:
```python
from gencall.core.capture import CaptureManager
loops_api.capture_manager = CaptureManager(
    command=config.capture_command, capture_dir=config.capture_dir,
    max_seconds=config.capture_max_seconds, max_mb=config.capture_max_mb,
    snaplen=config.capture_snaplen,
)
```

- [ ] **Step 4: Run → passes.** `python -m pytest tests/test_capture_api.py -v`. Also `python -c "from gencall.main import create_app; create_app(); print('ok')"`.

- [ ] **Step 5: Commit**

```bash
git add gencall/api/loops.py gencall/main.py tests/test_capture_api.py
git commit -m "feat(trace): worker per-loop capture endpoints + wiring"
```

---

## Task 4: Controller fleet-capture endpoints (route by box, stream download)

**Files:** Modify `gencall/api/loops.py`; Test `tests/test_capture_api.py`.

The UI talks to the controller; these resolve `box` (="local" → local manager; else a worker `api_url` → proxy). Mirror the existing `fleet_stop_loop` pattern (`loops.py:380`) for box resolution + `_worker_post`; for the download, stream via `httpx.Client.stream`.

- [ ] **Step 1: Failing test** — append to `tests/test_capture_api.py` a test that, with `box="local"`, the fleet endpoints delegate to the local manager (reuse the `client` fixture; assert `/api/loops/fleet-capture/start` with `{campaign_id:"loop-x", box:"local"}` returns a capture and `fleet-capture/list` shows it). (Remote/proxy path is covered by the existing `_worker_post` pattern; a local-path test is sufficient here.)

```python
def test_fleet_capture_local(client):
    r = client.post("/api/loops/fleet-capture/start", json={"campaign_id": "loop-x", "box": "local"})
    assert r.status_code == 200, r.text
    cid = r.json()["capture"]["id"]
    lst = client.get("/api/loops/fleet-capture/list", params={"campaign_id": "loop-x", "box": "local"})
    assert any(c["id"] == cid for c in lst.json()["captures"])
    assert client.post("/api/loops/fleet-capture/stop",
                       json={"campaign_id": "loop-x", "box": "local", "capture_id": cid}).status_code == 200
    assert client.request("DELETE", "/api/loops/fleet-capture/delete",
                          json={"campaign_id": "loop-x", "box": "local", "capture_id": cid}).status_code == 200
```

- [ ] **Step 2: Run → fails.**

- [ ] **Step 3: Implement** — in `gencall/api/loops.py`, add request models + endpoints. For remote boxes use the existing `_worker_post` (sync) for start/stop/list/delete, and a streaming `httpx.Client.stream` for download. Use the existing `Server`/`_db()` lookup (as in `fleet_stop_loop`) to get the worker `api_key`:

```python
class FleetCaptureReq(BaseModel):
    campaign_id: str
    box: str = "local"
    capture_id: str = ""


def _worker_key(box: str) -> str:
    from gencall.db.models import Server
    session = _db().get_session()
    try:
        s = session.query(Server).filter_by(api_url=box).first()
        return s.api_key if s else ""
    finally:
        session.close()


@router.post("/api/loops/fleet-capture/start", dependencies=[Depends(require_api_key)])
def fleet_capture_start(req: FleetCaptureReq):
    if not req.box or req.box == "local":
        return capture_start(req.campaign_id)              # reuse the worker handler
    return _worker_post(req.box, _worker_key(req.box),
                        f"/api/loops/{req.campaign_id}/capture/start", {})


@router.post("/api/loops/fleet-capture/stop", dependencies=[Depends(require_api_key)])
def fleet_capture_stop(req: FleetCaptureReq):
    if not req.box or req.box == "local":
        return capture_stop(req.campaign_id, req.capture_id)
    return _worker_post(req.box, _worker_key(req.box),
                        f"/api/loops/{req.campaign_id}/capture/{req.capture_id}/stop", {})


@router.get("/api/loops/fleet-capture/list", dependencies=[Depends(require_api_key)])
def fleet_capture_list(campaign_id: str, box: str = "local"):
    if not box or box == "local":
        return capture_list(campaign_id)
    import httpx
    with httpx.Client(verify=False, timeout=15.0) as c:
        r = c.get(box.rstrip("/") + f"/api/loops/{campaign_id}/captures",
                  headers={"X-API-Key": _worker_key(box)})
    r.raise_for_status()
    return r.json()


@router.delete("/api/loops/fleet-capture/delete", dependencies=[Depends(require_api_key)])
def fleet_capture_delete(req: FleetCaptureReq):
    if not req.box or req.box == "local":
        return capture_delete(req.campaign_id, req.capture_id)
    import httpx
    with httpx.Client(verify=False, timeout=15.0) as c:
        r = c.request("DELETE", req.box.rstrip("/") +
                      f"/api/loops/{req.campaign_id}/capture/{req.capture_id}",
                      headers={"X-API-Key": _worker_key(req.box)})
    r.raise_for_status()
    return r.json() if r.content else {"status": "deleted"}


@router.get("/api/loops/fleet-capture/download", dependencies=[Depends(require_api_key)])
def fleet_capture_download(campaign_id: str, capture_id: str, box: str = "local"):
    if not box or box == "local":
        return capture_download(campaign_id, capture_id)
    import httpx
    fname = f"{campaign_id}_{capture_id}.pcap"

    def _proxy():
        with httpx.Client(verify=False, timeout=None) as c:
            with c.stream("GET", box.rstrip("/") +
                          f"/api/loops/{campaign_id}/capture/{capture_id}/download",
                          headers={"X-API-Key": _worker_key(box)}) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes(65536):
                    yield chunk

    return StreamingResponse(_proxy(), media_type="application/vnd.tcpdump.pcap",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})
```

- [ ] **Step 4: Run → passes.** `python -m pytest tests/test_capture_api.py -v`; then `python -m pytest -q` green except the 2 known pre-existing E.164 failures.

- [ ] **Step 5: Commit**

```bash
git add gencall/api/loops.py tests/test_capture_api.py
git commit -m "feat(trace): controller fleet-capture endpoints (route by box, stream download)"
```

---

## Task 5: Frontend types + API client

**Files:** Modify `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`.

- [ ] **Step 1: Types** — in `types.ts`:

```typescript
export interface CaptureInfo {
  id: string;
  campaign_id: string;
  running: boolean;
  size_bytes: number;
  started_at: number | null;
  stopped_at: number | null;
}
```

- [ ] **Step 2: API client** — add to the `api` object in `api.ts` (download returns a URL the browser fetches — but it needs the X-API-Key, so reuse the authenticated-download approach; if `downloadAuthed` was removed in Part 2, add a small local authed-download helper or stream via fetch+blob):

```typescript
  startCapture: (campaign_id: string, box: string) =>
    request<{ status: string; capture: CaptureInfo }>("/api/loops/fleet-capture/start",
      { method: "POST", body: { campaign_id, box } }),
  stopCapture: (campaign_id: string, box: string, capture_id: string) =>
    request<{ status: string; capture: CaptureInfo }>("/api/loops/fleet-capture/stop",
      { method: "POST", body: { campaign_id, box, capture_id } }),
  listCaptures: (campaign_id: string, box: string) =>
    request<{ captures: CaptureInfo[] }>(
      `/api/loops/fleet-capture/list?campaign_id=${encodeURIComponent(campaign_id)}&box=${encodeURIComponent(box)}`),
  deleteCapture: (campaign_id: string, box: string, capture_id: string) =>
    request<{ status: string }>("/api/loops/fleet-capture/delete",
      { method: "DELETE", body: { campaign_id, box, capture_id } }),
  downloadCapture: (campaign_id: string, box: string, capture_id: string) =>
    downloadAuthed(
      `/api/loops/fleet-capture/download?campaign_id=${encodeURIComponent(campaign_id)}` +
        `&box=${encodeURIComponent(box)}&capture_id=${encodeURIComponent(capture_id)}`,
      `${campaign_id}_${capture_id}.pcap`),
```

> If `downloadAuthed` no longer exists (removed in Part 2), re-add this small helper (authenticated fetch → blob → synthetic `<a download>`):
> ```typescript
> async function downloadAuthed(path: string, filename: string): Promise<void> {
>   const headers: Record<string, string> = {};
>   const k = getApiKey(); if (k) headers["X-API-Key"] = k;
>   const res = await fetch(BASE + path, { headers });
>   if (!res.ok) throw new ApiError(res.status, res.statusText);
>   const url = URL.createObjectURL(await res.blob());
>   try { const a = document.createElement("a"); a.href = url; a.download = filename;
>         document.body.appendChild(a); a.click(); a.remove(); }
>   finally { URL.revokeObjectURL(url); }
> }
> ```

Add `CaptureInfo` to the type-import block.

- [ ] **Step 3: Verify** `cd frontend && npm run typecheck` (exit 0).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/lib/api.ts
git commit -m "feat(trace): frontend types + capture API client"
```

---

## Task 6: Frontend — capture controls + captures list on a running loop

**Files:** Modify `frontend/src/pages/Loops.tsx`.

- [ ] **Step 1: Implement** — on each running loop's action area (next to Stop), add a **Capture** button that opens a small modal (or inline panel) for that campaign:
  - "Start capture" / "Stop capture" buttons (call `api.startCapture(c.id, c.box ?? "local")` / `stopCapture`), refreshing a `listCaptures(c.id, c.box ?? "local")` view;
  - a list of captures (running?, size via a `bytes()`-style formatter, started time) each with **Download** (`api.downloadCapture(...)`) and **Delete** (`api.deleteCapture(...)`) buttons;
  - toasts on success/error matching the page's existing `useToast` usage.

Match the page's existing Modal/Button/Field/useToast/useAsync patterns. Keep capture controls visible only while the loop is running (use the campaign `status`).

- [ ] **Step 2: Verify** `cd frontend && npm run typecheck` (exit 0). Manual (Linux worker w/ tcpdump): Start capture on a running loop → Captures list shows it growing → Stop → Download pulls a `.pcap` openable in Wireshark → Delete removes it.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/Loops.tsx
git commit -m "feat(trace): capture controls + captures list on the Loops page"
```

---

## Task 7: Ensure tcpdump on workers (install + privilege)

**Files:** Modify `deploy/install.sh`, `deploy/install-ubuntu.sh`, `deploy/install-offline.sh`.

- [ ] **Step 1: Install tcpdump + grant capture privilege** — in each installer, where packages are installed and where `setcap cap_net_raw+ep` is granted to `sipp`, ensure `tcpdump` is installed and grant it capture caps so the non-root `gencall` user can run it. Add (matching each script's package step + the existing sipp setcap block):

```bash
# tcpdump for on-demand pcap capture (Trace). Grant capture caps so the
# non-root service user can run it without sudo.
command -v tcpdump >/dev/null 2>&1 || pkg_install tcpdump || true   # use each script's installer
if command -v setcap >/dev/null 2>&1 && command -v tcpdump >/dev/null 2>&1; then
  setcap cap_net_raw,cap_net_admin+eip "$(command -v tcpdump)" 2>/dev/null \
    && ok "granted capture caps to tcpdump" \
    || warn "could not setcap tcpdump — captures need: sudo setcap cap_net_raw,cap_net_admin+eip \$(command -v tcpdump)"
fi
```

Use each script's actual package-install helper (`pkg_install` / `apt install` / the offline wheelhouse path — for offline, tcpdump should already be on the base image; just do the setcap + a warn if absent).

- [ ] **Step 2: Verify** `bash -n deploy/install.sh deploy/install-ubuntu.sh deploy/install-offline.sh` (syntax). `grep -n tcpdump deploy/*.sh` shows the new lines.

- [ ] **Step 3: Commit**

```bash
git add deploy/install.sh deploy/install-ubuntu.sh deploy/install-offline.sh
git commit -m "feat(trace): install tcpdump + grant capture caps on workers"
```

---

## Self-Review

- **Spec coverage (spec §4):** on-demand start/stop (Tasks 3-4, 6) ✓; worker-local file (capture_dir, Task 2) ✓; pull-on-request streamed download (Task 4) ✓; list/delete (Tasks 3-4, 6) ✓; size/duration guardrails (watchdog, Task 2) ✓; filter scoped to dest switch + ports (Task 2/3) ✓; privilege/availability honest-fail + install setcap (Tasks 2, 7) ✓; never automatic (no auto-start anywhere) ✓.
- **Placeholder scan:** core code (capture.py, endpoints, filter, stream proxy, config) is complete. Tasks 6 (UI modal) and 7 (per-script package step) describe the change precisely and defer per-file detail to the implementer reading the actual file — precise instructions, not vague TODOs.
- **Type/contract consistency:** worker `capture` dict (`{id, campaign_id, running, size_bytes, started_at, stopped_at}`) == frontend `CaptureInfo`; fleet endpoints take `{campaign_id, box, capture_id}`; download streams `application/vnd.tcpdump.pcap`. `capture_manager`/`_capture_mgr()` names consistent across Tasks 2-4.
- **Cross-platform:** tcpdump absent (Windows dev) → `start()` raises `RuntimeError` → 503 (not a crash); tests use `fake_tcpdump.py`, so the suite is green on Windows.

## Open Items
- Spec §4.6: tcpdump privilege is environmental; Task 7 sets it on Linux installs. A worker without the cap returns a clear 503 from `capture/start`.
- Captures persist until deleted; the watchdog bounds a single capture's size/duration but not total disk across many kept captures — document "delete traces you don't need" (and a future periodic GC could cap total capture storage).

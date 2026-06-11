# GenCall NOC Console

The web control surface for GenCall v2.0 — a SIP/VoIP traffic generator. This
is a Vite + React + TypeScript single-page app with a "tungsten exchange"
aesthetic — ink-graphite plates, warm ivory type, one ember-orange lamp for
everything live — replacing the previous monolithic `gencall/web/dashboard.py`
(1,465 lines of inline HTML/CSS/JS).

## Why this exists

The old dashboard was one giant Python string with no build system, no tests,
and it polled the REST API every 2 s even though a full WebSocket streaming
layer (`gencall/api/websocket.py`) already existed but was never mounted. This
rebuild:

- Splits the UI into real, testable modules with a Vite build.
- Uses the **WebSocket streams** for live data (stats + logs) instead of polling.
- Adds a typed API client mirroring the FastAPI contract.
- Ships an in-browser **mock backend** so the UI is fully demoable with no server.
- Is keyboard-accessible (focus-trapped modals, focus-visible rings, ARIA live regions).

## Quick start

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173/console/  (mock data, no backend needed)
```

`npm run dev` proxies `/api` and `/ws` to `http://127.0.0.1:8080` (the
`gencall-server` default). Override with `GENCALL_BACKEND=http://host:port`.

### Mock vs live data

A small stateful SIP-traffic simulator (`src/lib/mock.ts`) drives the UI when no
backend is present. It is **on by default in dev** and **off in production builds**.

- `VITE_MOCK=false npm run dev` — force the dev server to talk to the real backend.
- `VITE_MOCK=true npm run build` — build a mock/demo bundle (e.g. for static hosting).

## Build & backend integration

```bash
npm run build      # emits to ../gencall/web/console/
```

`gencall/main.py` serves that directory as static files at `/console`, redirects
`/` → `/console/`, and mounts the WebSocket router. The legacy dashboard is kept
at `/legacy` as a fallback during transition. If the console build is absent,
the server logs a warning and falls back to the legacy dashboard at `/`.

Because the app uses a **hash router**, no server-side route rewriting is needed —
a single `index.html` handles every view.

## Scripts

| Command | Description |
|---|---|
| `npm run dev` | Dev server with HMR + API/WS proxy |
| `npm run build` | Type-check then bundle into `../gencall/web/console/` |
| `npm run preview` | Preview the production bundle |
| `npm run test` | Run the Vitest unit suite |
| `npm run lint` | ESLint |
| `npm run typecheck` | `tsc` with no emit |

## Layout

```
src/
  lib/        types.ts · api.ts (REST) · ws.ts (streams) · mock.ts · format.ts
  hooks/      useAsync (fetch/poll) · useStream (live topics + rolling stats)
  components/
    layout/   Shell · Sidebar · TopBar
    ui/       Panel · Button · Badge · StatTile · Modal · Toast · Misc
    charts/   TimeSeriesChart (canvas oscilloscope) · Sparkline · RadialGauge
  pages/      Dashboard · Campaigns · Scenarios · Connectors · Scheduler ·
              Console · Performance · History · Config
  styles/     tokens.css (design system) · global.css (atmosphere/reset)
```

### Design system

All color, type, spacing, and effect values live as CSS custom properties in
`src/styles/tokens.css`. Color is strictly semantic: ember `--signal` for
live/hot traffic, `--ok` green for healthy, `--amber` caution, `--crit` alarm,
`--cyan` info. Fonts: **Archivo** (headings, readouts) + **IBM Plex Mono**
(data). Charts are hand-rolled on `<canvas>` (DPR-correct,
no chart lib) for full control of the lamp glow and real-time performance.

## Known gaps / next steps

- **Scheduler** is a working UI shell with local state — the engine
  (`gencall/core/scheduler.py`) has no REST route yet. Wire `/api/scheduler`
  to persist jobs server-side.
- **CDR / SIP / alert streams** are defined in the WS layer; the console
  currently consumes `stats` and `logs`. Add panels for the rest as needed.
- **API authentication** is enforced server-side (`X-API-Key` on every endpoint
  except `/api/health`). The client attaches the key from `localStorage` via
  `getApiKey()` / `setApiKey()` in `src/lib/api.ts`. Mint a key with
  `gencall keys create` and store it with `setApiKey()` (a settings UI to enter
  it is still TODO).

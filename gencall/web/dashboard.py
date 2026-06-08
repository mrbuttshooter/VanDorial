"""
GenCall Web Dashboard - Professional Management Interface
Multi-page SPA with sidebar navigation, canvas charts, toast notifications.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');

:root {
  --bg: #0a0d14;
  --bg2: #0f1219;
  --sidebar: #0d1017;
  --card: #131720;
  --card-hover: #171c28;
  --border: #1e2433;
  --border-light: #2a3045;
  --accent: #00d4aa;
  --accent-glow: rgba(0,212,170,.15);
  --accent2: #7c5cfc;
  --accent2-glow: rgba(124,92,252,.15);
  --danger: #ff4757;
  --danger-glow: rgba(255,71,87,.12);
  --warn: #ffb347;
  --warn-glow: rgba(255,179,71,.12);
  --text: #e8eaf0;
  --text2: #6b7280;
  --text3: #4a5063;
  --success: #00d4aa;
  --hover: #181d2a;
  --ring: rgba(0,212,170,.25);
  --shadow: 0 4px 24px rgba(0,0,0,.35);
  --shadow-lg: 0 8px 40px rgba(0,0,0,.5);
}

* { margin:0; padding:0; box-sizing:border-box; }

body {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text);
  display: flex;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

/* Scrollbar */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-light); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--text3); }

/* ═══ SIDEBAR ═══ */
.sidebar {
  width: 240px;
  background: var(--sidebar);
  border-right: 1px solid var(--border);
  position: fixed;
  top: 0; left: 0;
  height: 100vh;
  display: flex;
  flex-direction: column;
  z-index: 100;
  transition: width .2s ease;
}

.sidebar .logo {
  padding: 24px 20px 20px;
  border-bottom: 1px solid var(--border);
}

.sidebar .logo h1 {
  font-size: 22px;
  font-weight: 900;
  letter-spacing: -0.5px;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  margin-bottom: 2px;
}

.sidebar .logo span {
  font-size: 11px;
  color: var(--text3);
  font-weight: 500;
  letter-spacing: 0.3px;
}

.sidebar nav { flex: 1; padding: 16px 0; overflow-y: auto; }

.sidebar nav a {
  display: flex;
  align-items: center;
  padding: 10px 20px;
  color: var(--text2);
  text-decoration: none;
  font-size: 13px;
  font-weight: 500;
  border-left: 3px solid transparent;
  transition: all .15s ease;
  gap: 12px;
  position: relative;
}

.sidebar nav a:hover { background: var(--hover); color: var(--text); }

.sidebar nav a.active {
  color: var(--accent);
  border-left-color: var(--accent);
  background: var(--accent-glow);
}

.sidebar nav a svg { width: 18px; height: 18px; flex-shrink: 0; opacity: .6; }
.sidebar nav a.active svg { opacity: 1; }

.nav-section {
  padding: 20px 20px 8px;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 2px;
  color: var(--text3);
  font-weight: 700;
}

.sidebar-footer {
  padding: 16px 20px;
  border-top: 1px solid var(--border);
  font-size: 11px;
  color: var(--text3);
}

/* ═══ MAIN ═══ */
.main { margin-left: 240px; flex: 1; min-height: 100vh; }

.topbar {
  background: rgba(13,16,23,.85);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
  padding: 0 28px;
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 50;
}

.topbar h2 { font-size: 16px; font-weight: 700; letter-spacing: -0.3px; }

.topbar-right { display: flex; align-items: center; gap: 16px; }

.topbar .status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text2);
  font-weight: 500;
}

.topbar .dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--success);
  box-shadow: 0 0 8px var(--accent-glow);
  animation: pulse-dot 2s ease infinite;
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; }
  50% { opacity: .5; }
}

.topbar .clock { font-size: 12px; color: var(--text3); font-weight: 500; font-variant-numeric: tabular-nums; }

.content { padding: 24px 28px; }

/* ═══ ANIMATIONS ═══ */
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

@keyframes slideIn {
  from { opacity: 0; transform: scale(.96); }
  to { opacity: 1; transform: scale(1); }
}

@keyframes shimmer {
  0% { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}

.page { display: none; }
.page.active { display: block; animation: fadeIn .25s ease; }

/* ═══ STAT CARDS ═══ */
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }

.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
  transition: all .2s ease;
  position: relative;
  overflow: hidden;
}

.card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  opacity: 0;
  transition: opacity .2s;
}

.card:hover { border-color: var(--border-light); transform: translateY(-1px); }
.card:hover::before { opacity: 1; }

.card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }

.card h4 {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--text2);
  font-weight: 600;
}

.card-icon {
  width: 32px; height: 32px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.card-icon svg { width: 16px; height: 16px; }
.card-icon.green { background: var(--accent-glow); color: var(--accent); }
.card-icon.purple { background: var(--accent2-glow); color: var(--accent2); }
.card-icon.red { background: var(--danger-glow); color: var(--danger); }
.card-icon.orange { background: var(--warn-glow); color: var(--warn); }

.card .val { font-size: 28px; font-weight: 800; letter-spacing: -1px; font-variant-numeric: tabular-nums; }
.card .val.g { color: var(--success); }
.card .val.r { color: var(--danger); }
.card .val.p { color: var(--accent2); }
.card .val.o { color: var(--warn); }

.card .sub { font-size: 11px; color: var(--text3); margin-top: 4px; font-weight: 500; }

.mini-spark {
  display: flex;
  align-items: flex-end;
  gap: 1px;
  height: 24px;
  margin-top: 8px;
}

.mini-spark .bar {
  flex: 1;
  background: var(--accent);
  border-radius: 1px;
  min-height: 2px;
  opacity: .5;
  transition: height .3s;
}

/* ═══ TABLES ═══ */
.tbl-wrap {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
  margin-bottom: 20px;
}

.tbl-head {
  padding: 16px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border);
}

.tbl-head h3 { font-size: 14px; font-weight: 700; }

.tbl-search {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 12px;
  color: var(--text);
  font-size: 12px;
  outline: none;
  width: 200px;
  transition: border-color .15s;
}

.tbl-search:focus { border-color: var(--accent); }
.tbl-search::placeholder { color: var(--text3); }

table { width: 100%; border-collapse: collapse; }

th {
  text-align: left;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--text3);
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  font-weight: 700;
  background: var(--bg2);
}

td {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  font-weight: 500;
}

tr { transition: background .1s; }
tr:hover { background: rgba(255,255,255,.02); }
tr:last-child td { border-bottom: none; }

.empty-row {
  text-align: center;
  color: var(--text3);
  padding: 48px 16px !important;
  font-size: 13px;
}

.empty-row svg { width: 40px; height: 40px; opacity: .2; margin-bottom: 12px; }

/* ═══ BUTTONS ═══ */
.btn {
  padding: 8px 16px;
  border: none;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all .15s ease;
  font-family: inherit;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.btn-primary { background: var(--accent); color: #000; }
.btn-primary:hover { background: #00e8ba; box-shadow: 0 0 20px var(--accent-glow); }

.btn-danger { background: var(--danger); color: #fff; }
.btn-danger:hover { background: #ff6b7a; box-shadow: 0 0 20px var(--danger-glow); }

.btn-warn { background: var(--warn); color: #000; }

.btn-sm { padding: 5px 12px; font-size: 11px; border-radius: 6px; }

.btn-outline {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text2);
}
.btn-outline:hover { border-color: var(--text); color: var(--text); background: var(--hover); }

.btn-group { display: flex; gap: 6px; }

.btn-ghost { background: none; border: none; color: var(--text2); cursor: pointer; padding: 4px 8px; border-radius: 4px; }
.btn-ghost:hover { color: var(--text); background: var(--hover); }

/* ═══ BADGES ═══ */
.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 12px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .3px;
}

.badge::before {
  content: '';
  width: 6px; height: 6px;
  border-radius: 50%;
}

.badge-run { background: rgba(0,212,170,.1); color: var(--success); }
.badge-run::before { background: var(--success); }
.badge-stop { background: rgba(107,114,128,.1); color: var(--text2); }
.badge-stop::before { background: var(--text2); }
.badge-err { background: rgba(255,71,87,.1); color: var(--danger); }
.badge-err::before { background: var(--danger); }
.badge-idle { background: rgba(124,92,252,.1); color: var(--accent2); }
.badge-idle::before { background: var(--accent2); }

/* ═══ CHARTS ═══ */
.chart-box {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
  margin-bottom: 20px;
}

.chart-box h3 {
  font-size: 12px;
  color: var(--text2);
  margin-bottom: 16px;
  text-transform: uppercase;
  letter-spacing: 1px;
  font-weight: 700;
}

.chart-canvas { width: 100%; height: 180px; display: block; border-radius: 4px; }

/* ═══ MODALS ═══ */
.modal-bg {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,.7);
  backdrop-filter: blur(4px);
  z-index: 200;
  align-items: center;
  justify-content: center;
}

.modal-bg.show { display: flex; }

.modal {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  width: 560px;
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: var(--shadow-lg);
  animation: slideIn .2s ease;
}

.modal-head {
  padding: 20px 24px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.modal-head h3 { font-size: 16px; font-weight: 700; }

.modal-close {
  background: var(--hover);
  border: none;
  color: var(--text2);
  font-size: 18px;
  cursor: pointer;
  width: 32px; height: 32px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all .15s;
}

.modal-close:hover { background: var(--border); color: var(--text); }

.modal-body { padding: 24px; }

.modal-foot {
  padding: 16px 24px;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

/* ═══ FORMS ═══ */
.fg { margin-bottom: 16px; }

.fg label {
  display: block;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .8px;
  color: var(--text2);
  margin-bottom: 6px;
  font-weight: 600;
}

.fg input, .fg select, .fg textarea {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text);
  padding: 10px 14px;
  font-size: 13px;
  font-family: inherit;
  outline: none;
  transition: border-color .15s, box-shadow .15s;
}

.fg input:focus, .fg select:focus, .fg textarea:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--ring);
}

.fg textarea { min-height: 120px; font-family: 'Fira Code', Consolas, monospace; font-size: 12px; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }

/* ═══ CONSOLE ═══ */
.console {
  background: #060810;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
  font-family: 'Fira Code', Consolas, monospace;
  font-size: 12px;
  line-height: 1.8;
  max-height: 500px;
  overflow-y: auto;
  color: var(--text3);
}

.console .log-info { color: var(--accent); }
.console .log-warn { color: var(--warn); }
.console .log-err { color: var(--danger); }
.console .log-line { padding: 1px 0; }

/* ═══ TOAST ═══ */
.toast-container {
  position: fixed;
  top: 68px;
  right: 20px;
  z-index: 300;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.toast {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 16px;
  font-size: 13px;
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 10px;
  box-shadow: var(--shadow-lg);
  animation: toastIn .3s ease;
  min-width: 280px;
  max-width: 400px;
}

.toast.toast-success { border-left: 3px solid var(--success); }
.toast.toast-error { border-left: 3px solid var(--danger); }
.toast.toast-warn { border-left: 3px solid var(--warn); }
.toast.toast-info { border-left: 3px solid var(--accent2); }

.toast-icon { font-size: 16px; flex-shrink: 0; }

.toast.removing { animation: toastOut .25s ease forwards; }

@keyframes toastIn { from { opacity: 0; transform: translateX(40px); } to { opacity: 1; transform: translateX(0); } }
@keyframes toastOut { from { opacity: 1; transform: translateX(0); } to { opacity: 0; transform: translateX(40px); } }

/* ═══ RESPONSIVE ═══ */
@media (max-width: 1100px) {
  .cards { grid-template-columns: repeat(2, 1fr); }
  .form-row { grid-template-columns: 1fr; }
}

@media (max-width: 768px) {
  .sidebar { width: 60px; }
  .sidebar .logo span, .sidebar nav a span, .nav-section, .sidebar-footer { display: none; }
  .sidebar .logo { padding: 16px; text-align: center; }
  .sidebar nav a { justify-content: center; padding: 14px; gap: 0; }
  .main { margin-left: 60px; }
  .cards { grid-template-columns: 1fr; }
  .topbar h2 { font-size: 14px; }
}
"""

ICONS = {
    "dashboard": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></svg>',
    "campaigns": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
    "scenarios": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/></svg>',
    "connectors": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="5" r="3"/><circle cx="5" cy="19" r="3"/><circle cx="19" cy="19" r="3"/><path d="M12 8v2m-5 6l4-6m6 6l-4-6"/></svg>',
    "scheduler": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
    "console": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
    "history": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 8v4l3 3"/><circle cx="12" cy="12" r="10"/></svg>',
    "perf": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 20V10"/><path d="M12 20V4"/><path d="M6 20v-6"/></svg>',
    "config": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>',
}

SIDEBAR_ITEMS = [
    ("dashboard", "Dashboard", ICONS["dashboard"]),
    ("campaigns", "Test Campaigns", ICONS["campaigns"]),
    ("scenarios", "Scenarios", ICONS["scenarios"]),
    ("connectors", "Connectors", ICONS["connectors"]),
    ("scheduler", "Scheduler", ICONS["scheduler"]),
    ("console", "Console", ICONS["console"]),
    ("history", "History", ICONS["history"]),
    ("performance", "Performance", ICONS["perf"]),
    ("config", "Configuration", ICONS["config"]),
]

NAV_MAIN = "".join(
    f'<a href="#{id}" data-page="{id}" class="nav-link">{icon}<span>{label}</span></a>'
    for id, label, icon in SIDEBAR_ITEMS[:5]
)
NAV_MONITOR = "".join(
    f'<a href="#{id}" data-page="{id}" class="nav-link">{icon}<span>{label}</span></a>'
    for id, label, icon in SIDEBAR_ITEMS[5:]
)

HTML_BODY = f"""
<div class="toast-container" id="toasts"></div>

<div class="sidebar">
  <div class="logo">
    <h1>GenCall</h1>
    <span>SIP Traffic Generator v2.0</span>
  </div>
  <nav>
    <div class="nav-section">Main</div>
    {NAV_MAIN}
    <div class="nav-section">Monitor</div>
    {NAV_MONITOR}
  </nav>
  <div class="sidebar-footer">
    <span id="sidebarVersion">v2.0.0</span>
  </div>
</div>

<div class="main">
  <div class="topbar">
    <h2 id="pageTitle">Dashboard</h2>
    <div class="topbar-right">
      <div class="clock" id="clock"></div>
      <div class="status">
        <div class="dot" id="statusDot"></div>
        <span id="statusText">Connecting...</span>
      </div>
    </div>
  </div>
  <div class="content">

    <!-- DASHBOARD -->
    <div class="page active" id="pg-dashboard">
      <div class="cards">
        <div class="card">
          <div class="card-header">
            <h4>Active Tests</h4>
            <div class="card-icon purple"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div>
          </div>
          <div class="val p" id="s-active">0</div>
          <div class="sub">Running instances</div>
        </div>
        <div class="card">
          <div class="card-header">
            <h4>Calls / Second</h4>
            <div class="card-icon green"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 17l5-5-5-5M6 17l5-5-5-5"/></svg></div>
          </div>
          <div class="val g" id="s-cps">0.00</div>
          <div class="mini-spark" id="spark-cps"></div>
        </div>
        <div class="card">
          <div class="card-header">
            <h4>Success Rate</h4>
            <div class="card-icon green"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></div>
          </div>
          <div class="val g" id="s-sr">0%</div>
          <div class="sub">Overall success</div>
        </div>
        <div class="card">
          <div class="card-header">
            <h4>Total Calls</h4>
            <div class="card-icon orange"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6A19.79 19.79 0 012.12 4.18 2 2 0 014.11 2h3a2 2 0 012 1.72c.127.96.361 1.903.7 2.81a2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0122 16.92z"/></svg></div>
          </div>
          <div class="val" id="s-total">0</div>
          <div class="sub" id="s-total-sub">Since start</div>
        </div>
      </div>

      <div class="cards">
        <div class="card"><div class="card-header"><h4>Successful</h4></div><div class="val g" style="font-size:22px" id="s-ok">0</div></div>
        <div class="card"><div class="card-header"><h4>Failed</h4></div><div class="val r" style="font-size:22px" id="s-fail">0</div></div>
        <div class="card"><div class="card-header"><h4>Current Calls</h4></div><div class="val o" style="font-size:22px" id="s-cur">0</div></div>
        <div class="card"><div class="card-header"><h4>Avg Response</h4></div><div class="val" style="font-size:22px" id="s-rt">0ms</div></div>
      </div>

      <div class="chart-box">
        <h3>Calls Per Second (Live)</h3>
        <canvas class="chart-canvas" id="cpsChart"></canvas>
      </div>

      <div class="tbl-wrap">
        <div class="tbl-head">
          <h3>Running Tests</h3>
          <input class="tbl-search" placeholder="Search tests..." oninput="filterTable('dashTests',this.value)">
        </div>
        <table>
          <thead><tr><th>ID</th><th>Scenario</th><th>Target</th><th>Rate</th><th>Active</th><th>Total</th><th>OK</th><th>Fail</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody id="dashTests"></tbody>
        </table>
      </div>
    </div>

    <!-- TEST CAMPAIGNS -->
    <div class="page" id="pg-campaigns">
      <div style="margin-bottom:16px;display:flex;justify-content:space-between;align-items:center">
        <div></div>
        <button class="btn btn-primary" onclick="openModal('newTest')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          New Test Campaign
        </button>
      </div>
      <div class="tbl-wrap">
        <div class="tbl-head">
          <h3>Test Campaigns</h3>
          <input class="tbl-search" placeholder="Filter..." oninput="filterTable('campTable',this.value)">
        </div>
        <table>
          <thead><tr><th>Name</th><th>Scenario</th><th>Target</th><th>Rate</th><th>Limit</th><th>Max Calls</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody id="campTable"></tbody>
        </table>
      </div>
    </div>

    <!-- SCENARIOS -->
    <div class="page" id="pg-scenarios">
      <div style="margin-bottom:16px;display:flex;justify-content:space-between;align-items:center">
        <div></div>
        <button class="btn btn-primary" onclick="openModal('newScenario')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          New Scenario
        </button>
      </div>
      <div class="tbl-wrap">
        <div class="tbl-head"><h3>SIP Scenarios</h3></div>
        <table>
          <thead><tr><th>Name</th><th>Type</th><th>Description</th><th>Actions</th></tr></thead>
          <tbody id="scenTable"></tbody>
        </table>
      </div>
    </div>

    <!-- CONNECTORS -->
    <div class="page" id="pg-connectors">
      <div style="margin-bottom:16px;display:flex;justify-content:space-between;align-items:center">
        <div></div>
        <button class="btn btn-primary" onclick="openModal('newConnector')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          New Connector
        </button>
      </div>
      <div class="tbl-wrap">
        <div class="tbl-head"><h3>SIP Connectors</h3></div>
        <table>
          <thead><tr><th>Name</th><th>Local IP</th><th>Port</th><th>Remote IP</th><th>Port</th><th>Transport</th><th>Enabled</th><th>Actions</th></tr></thead>
          <tbody id="connTable"></tbody>
        </table>
      </div>
    </div>

    <!-- SCHEDULER -->
    <div class="page" id="pg-scheduler">
      <div style="margin-bottom:16px;display:flex;justify-content:space-between;align-items:center">
        <div></div>
        <button class="btn btn-primary" onclick="openModal('newSchedule')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          New Schedule
        </button>
      </div>
      <div class="tbl-wrap">
        <div class="tbl-head"><h3>Scheduled Tests</h3></div>
        <table>
          <thead><tr><th>Name</th><th>Scenario</th><th>Schedule</th><th>Next Run</th><th>Runs</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody id="schedTable"><tr><td colspan="7" class="empty-row">
            <div><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg></div>
            No scheduled tests yet
          </td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- CONSOLE -->
    <div class="page" id="pg-console">
      <div class="chart-box" style="padding:0;overflow:hidden">
        <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
          <h3 style="margin:0;text-transform:none;letter-spacing:0;font-size:14px">Live Console</h3>
          <div class="btn-group">
            <button class="btn btn-sm btn-outline" onclick="document.getElementById('consoleLog').scrollTop=document.getElementById('consoleLog').scrollHeight">Scroll Down</button>
            <button class="btn btn-sm btn-outline" onclick="clearConsole()">Clear</button>
          </div>
        </div>
        <div class="console" id="consoleLog" style="border:none;border-radius:0;max-height:calc(100vh - 220px)">
          <div class="log-line log-info">[GenCall] System initialized and ready.</div>
        </div>
      </div>
    </div>

    <!-- HISTORY -->
    <div class="page" id="pg-history">
      <div class="tbl-wrap">
        <div class="tbl-head">
          <h3>Test History</h3>
          <input class="tbl-search" placeholder="Search history..." oninput="filterTable('histTable',this.value)">
        </div>
        <table>
          <thead><tr><th>Name</th><th>Scenario</th><th>Status</th><th>Total</th><th>Success</th><th>Failed</th><th>Rate</th><th>Started</th></tr></thead>
          <tbody id="histTable"></tbody>
        </table>
      </div>
    </div>

    <!-- PERFORMANCE -->
    <div class="page" id="pg-performance">
      <div class="cards" style="grid-template-columns:repeat(3,1fr);margin-bottom:20px">
        <div class="card">
          <div class="card-header"><h4>Peak CPS</h4><div class="card-icon green"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/></svg></div></div>
          <div class="val g" id="p-peak">0</div>
        </div>
        <div class="card">
          <div class="card-header"><h4>Avg Success Rate</h4><div class="card-icon purple"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></div></div>
          <div class="val" id="p-asr">0%</div>
        </div>
        <div class="card">
          <div class="card-header"><h4>Avg Response Time</h4><div class="card-icon orange"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg></div></div>
          <div class="val" id="p-art">0ms</div>
        </div>
      </div>
      <div class="chart-box">
        <h3>CPS Over Time</h3>
        <canvas class="chart-canvas" id="perfChart" style="height:220px"></canvas>
      </div>
      <div class="chart-box">
        <h3>Success Rate Over Time</h3>
        <canvas class="chart-canvas" id="srChart" style="height:220px"></canvas>
      </div>
    </div>

    <!-- CONFIGURATION -->
    <div class="page" id="pg-config">
      <div class="cards" style="grid-template-columns:1fr 1fr">
        <div class="card">
          <h4 style="margin-bottom:16px">Server Information</h4>
          <table style="font-size:13px">
            <tr><td style="color:var(--text2);padding:6px 16px 6px 0;font-weight:500">Version</td><td id="c-ver" style="font-weight:600">-</td></tr>
            <tr><td style="color:var(--text2);padding:6px 16px 6px 0;font-weight:500">Status</td><td id="c-stat">-</td></tr>
            <tr><td style="color:var(--text2);padding:6px 16px 6px 0;font-weight:500">Active Tests</td><td id="c-tests">-</td></tr>
            <tr><td style="color:var(--text2);padding:6px 16px 6px 0;font-weight:500">API Docs</td><td><a href="/docs" target="_blank" style="color:var(--accent);text-decoration:none;font-weight:600">/docs (OpenAPI)</a></td></tr>
          </table>
        </div>
        <div class="card">
          <h4 style="margin-bottom:16px">Quick Actions</h4>
          <div style="display:flex;flex-direction:column;gap:10px">
            <button class="btn btn-danger" onclick="confirmStopAll()" style="width:100%;justify-content:center">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="3" y="3" width="18" height="18" rx="2"/></svg>
              Emergency Stop All
            </button>
            <button class="btn btn-outline" onclick="navigate('campaigns')" style="width:100%;justify-content:center">New Test Campaign</button>
            <button class="btn btn-outline" onclick="navigate('scenarios')" style="width:100%;justify-content:center">Manage Scenarios</button>
          </div>
        </div>
      </div>
    </div>

  </div>
</div>

<!-- MODALS -->
<div class="modal-bg" id="modal-newTest">
  <div class="modal">
    <div class="modal-head"><h3>New Test Campaign</h3><button class="modal-close" onclick="closeModals()">&times;</button></div>
    <div class="modal-body">
      <div class="form-row"><div class="fg"><label>Test Name</label><input id="f-name" placeholder="my-test-01"></div>
      <div class="fg"><label>Scenario</label><select id="f-scenario"></select></div></div>
      <div class="form-row"><div class="fg"><label>Remote Host</label><input id="f-host" placeholder="10.0.0.1"></div>
      <div class="fg"><label>Remote Port</label><input id="f-port" type="number" value="5060"></div></div>
      <div class="form-row"><div class="fg"><label>Transport</label><select id="f-transport"><option>udp</option><option>tcp</option><option>tls</option></select></div>
      <div class="fg"><label>Call Rate (cps)</label><input id="f-rate" type="number" value="1" step="0.1"></div></div>
      <div class="form-row"><div class="fg"><label>Concurrent Calls</label><input id="f-limit" type="number" value="10"></div>
      <div class="fg"><label>Max Calls (0=unlimited)</label><input id="f-max" type="number" value="0"></div></div>
      <div class="form-row"><div class="fg"><label>Duration (sec, 0=forever)</label><input id="f-dur" type="number" value="0"></div>
      <div class="fg"><label>Local IP (optional)</label><input id="f-lip" placeholder="auto"></div></div>
      <div class="form-row"><div class="fg"><label>Auth User</label><input id="f-au"></div>
      <div class="fg"><label>Auth Password</label><input id="f-ap" type="password"></div></div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-outline" onclick="closeModals()">Cancel</button>
      <button class="btn btn-primary" onclick="startTest()">Start Test</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="modal-newConnector">
  <div class="modal">
    <div class="modal-head"><h3>New Connector</h3><button class="modal-close" onclick="closeModals()">&times;</button></div>
    <div class="modal-body">
      <div class="fg"><label>Name</label><input id="fc-name" placeholder="my-pbx"></div>
      <div class="form-row"><div class="fg"><label>Local IP</label><input id="fc-lip" placeholder="10.0.0.1"></div>
      <div class="fg"><label>Local Port</label><input id="fc-lp" type="number" value="5060"></div></div>
      <div class="form-row"><div class="fg"><label>Remote IP</label><input id="fc-rip" placeholder="10.0.0.2"></div>
      <div class="fg"><label>Remote Port</label><input id="fc-rp" type="number" value="5060"></div></div>
      <div class="fg"><label>Transport</label><select id="fc-tr"><option>udp</option><option>tcp</option><option>tls</option></select></div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-outline" onclick="closeModals()">Cancel</button>
      <button class="btn btn-primary" onclick="createConnector()">Create Connector</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="modal-newScenario">
  <div class="modal">
    <div class="modal-head"><h3>New Scenario</h3><button class="modal-close" onclick="closeModals()">&times;</button></div>
    <div class="modal-body">
      <div class="form-row"><div class="fg"><label>Name</label><input id="fs-name" placeholder="my-scenario"></div>
      <div class="fg"><label>Mode</label><select id="fs-mode"><option value="uac">UAC (Client)</option><option value="uas">UAS (Server)</option></select></div></div>
      <div class="fg"><label>Description</label><input id="fs-desc" placeholder="What this scenario does"></div>
      <div class="fg"><label>XML Content</label><textarea id="fs-xml" placeholder="&lt;scenario&gt;...&lt;/scenario&gt;"></textarea></div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-outline" onclick="closeModals()">Cancel</button>
      <button class="btn btn-primary" onclick="createScenario()">Save Scenario</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="modal-viewScenario">
  <div class="modal" style="width:700px">
    <div class="modal-head"><h3 id="vs-title">Scenario</h3><button class="modal-close" onclick="closeModals()">&times;</button></div>
    <div class="modal-body">
      <pre style="background:var(--bg);padding:16px;border-radius:10px;overflow-x:auto;font-size:12px;line-height:1.7;max-height:500px;overflow-y:auto;border:1px solid var(--border)" id="vs-content"></pre>
    </div>
  </div>
</div>

<div class="modal-bg" id="modal-newSchedule">
  <div class="modal">
    <div class="modal-head"><h3>New Scheduled Test</h3><button class="modal-close" onclick="closeModals()">&times;</button></div>
    <div class="modal-body">
      <div class="fg"><label>Name</label><input id="fh-name" placeholder="nightly-test"></div>
      <div class="fg"><label>Scenario</label><select id="fh-scenario"></select></div>
      <div class="form-row"><div class="fg"><label>Remote Host</label><input id="fh-host" placeholder="10.0.0.1"></div>
      <div class="fg"><label>Interval (minutes)</label><input id="fh-int" type="number" value="30"></div></div>
      <div class="form-row"><div class="fg"><label>Call Rate</label><input id="fh-rate" type="number" value="1"></div>
      <div class="fg"><label>Max Calls</label><input id="fh-max" type="number" value="100"></div></div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-outline" onclick="closeModals()">Cancel</button>
      <button class="btn btn-primary" onclick="closeModals()">Create Schedule</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="modal-confirm">
  <div class="modal" style="width:420px">
    <div class="modal-head"><h3 id="confirm-title">Confirm</h3><button class="modal-close" onclick="closeModals()">&times;</button></div>
    <div class="modal-body"><p id="confirm-msg" style="color:var(--text2);line-height:1.6"></p></div>
    <div class="modal-foot">
      <button class="btn btn-outline" onclick="closeModals()">Cancel</button>
      <button class="btn btn-danger" id="confirm-btn" onclick="closeModals()">Confirm</button>
    </div>
  </div>
</div>
"""

JS = """
let cpsH=[], srH=[], sparkCps=[], currentPage='dashboard';

const titles = {
  dashboard:'Dashboard', campaigns:'Test Campaigns', scenarios:'Scenarios',
  connectors:'Connectors', scheduler:'Scheduler', console:'Console',
  history:'History', performance:'Performance', config:'Configuration'
};

// ─── Toast Notifications ──────────────────────────────────────────
function toast(msg, type='info') {
  const c = document.getElementById('toasts');
  const t = document.createElement('div');
  t.className = 'toast toast-' + type;
  const icons = { success:'\\u2713', error:'\\u2717', warn:'\\u26A0', info:'\\u2139' };
  t.innerHTML = '<span class="toast-icon">' + (icons[type]||'') + '</span><span>' + msg + '</span>';
  c.appendChild(t);
  setTimeout(() => { t.classList.add('removing'); setTimeout(() => t.remove(), 250); }, 3500);
}

// ─── Navigation ──────────────────────────────────────────────────
function navigate(page) {
  currentPage = page;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(a => a.classList.remove('active'));
  const pg = document.getElementById('pg-' + page);
  if (pg) pg.classList.add('active');
  const nl = document.querySelector('[data-page="' + page + '"]');
  if (nl) nl.classList.add('active');
  document.getElementById('pageTitle').textContent = titles[page] || page;
  if (page === 'scenarios') loadScenarios();
  if (page === 'connectors') loadConnectors();
  if (page === 'history') loadHistory();
  if (page === 'config') loadConfig();
}

window.addEventListener('hashchange', () => { const h = location.hash.slice(1); if (h) navigate(h); });
window.addEventListener('load', () => {
  const h = location.hash.slice(1);
  navigate(h || 'dashboard');
  loadScenarioSelects();
  updateClock();
  setInterval(updateClock, 1000);
});

document.querySelectorAll('.nav-link').forEach(a => a.addEventListener('click', e => {
  e.preventDefault();
  navigate(a.dataset.page);
  history.pushState(null, null, '#' + a.dataset.page);
}));

// ─── Clock ───────────────────────────────────────────────────────
function updateClock() {
  const d = new Date();
  document.getElementById('clock').textContent =
    d.toLocaleDateString(undefined, {month:'short',day:'numeric'}) + '  ' +
    d.toLocaleTimeString(undefined, {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

// ─── API ─────────────────────────────────────────────────────────
async function api(method, path, body) {
  const o = { method, headers: {'Content-Type': 'application/json'} };
  if (body) o.body = JSON.stringify(body);
  const r = await fetch('/api' + path, o);
  if (!r.ok) {
    const e = await r.json().catch(() => ({detail: r.statusText}));
    throw new Error(e.detail || 'Request failed');
  }
  return r.json();
}

// ─── Modals ──────────────────────────────────────────────────────
function openModal(id) { document.getElementById('modal-' + id).classList.add('show'); }
function closeModals() { document.querySelectorAll('.modal-bg').forEach(m => m.classList.remove('show')); }

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModals(); });

document.querySelectorAll('.modal-bg').forEach(bg => {
  bg.addEventListener('click', e => { if (e.target === bg) closeModals(); });
});

// ─── Confirm Dialog ──────────────────────────────────────────────
function showConfirm(title, msg, onOk) {
  document.getElementById('confirm-title').textContent = title;
  document.getElementById('confirm-msg').textContent = msg;
  const btn = document.getElementById('confirm-btn');
  btn.onclick = () => { closeModals(); onOk(); };
  openModal('confirm');
}

function confirmStopAll() {
  showConfirm('Stop All Tests', 'This will immediately stop all running test instances. Are you sure?', stopAllTests);
}

// ─── Table Filter ────────────────────────────────────────────────
function filterTable(tbodyId, query) {
  const rows = document.getElementById(tbodyId).querySelectorAll('tr');
  const q = query.toLowerCase();
  rows.forEach(r => {
    r.style.display = r.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

// ─── Console ─────────────────────────────────────────────────────
function clog(msg, cls='') {
  const c = document.getElementById('consoleLog');
  const t = new Date().toLocaleTimeString();
  const div = document.createElement('div');
  div.className = 'log-line ' + cls;
  div.innerHTML = '[' + t + '] ' + msg;
  c.appendChild(div);
  if (c.children.length > 500) c.removeChild(c.firstChild);
  c.scrollTop = c.scrollHeight;
}
function clearConsole() { document.getElementById('consoleLog').innerHTML = ''; }

// ─── Badge ───────────────────────────────────────────────────────
function badge(state) {
  const m = { running:'badge-run', stopped:'badge-stop', error:'badge-err', idle:'badge-idle', completed:'badge-stop', failed:'badge-err' };
  return '<span class="badge ' + (m[state]||'badge-idle') + '">' + state + '</span>';
}

// ─── Canvas Chart ────────────────────────────────────────────────
function drawChart(canvasId, data, color, fillAlpha) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !data.length) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const w = rect.width, h = rect.height;
  const pad = { top: 8, right: 8, bottom: 24, left: 44 };
  const cw = w - pad.left - pad.right;
  const ch = h - pad.top - pad.bottom;
  const mx = Math.max(...data, 1);

  ctx.clearRect(0, 0, w, h);

  // Grid lines
  ctx.strokeStyle = 'rgba(255,255,255,.04)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (ch / 4) * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(w - pad.right, y);
    ctx.stroke();
    ctx.fillStyle = 'rgba(255,255,255,.2)';
    ctx.font = '10px Inter, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText((mx - (mx / 4) * i).toFixed(1), pad.left - 8, y + 3);
  }

  if (data.length < 2) return;

  // Line
  ctx.beginPath();
  const step = cw / (data.length - 1);
  for (let i = 0; i < data.length; i++) {
    const x = pad.left + step * i;
    const y = pad.top + ch - (data[i] / mx) * ch;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.stroke();

  // Fill
  ctx.lineTo(pad.left + step * (data.length - 1), pad.top + ch);
  ctx.lineTo(pad.left, pad.top + ch);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + ch);
  grad.addColorStop(0, color.replace(')', ',' + fillAlpha + ')').replace('rgb', 'rgba'));
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = grad;
  ctx.fill();

  // Glow dot on last point
  const lastX = pad.left + step * (data.length - 1);
  const lastY = pad.top + ch - (data[data.length - 1] / mx) * ch;
  ctx.beginPath();
  ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(lastX, lastY, 6, 0, Math.PI * 2);
  ctx.fillStyle = color.replace(')', ',0.2)').replace('rgb', 'rgba');
  ctx.fill();
}

// Mini sparkline
function renderSpark(elId, data) {
  const el = document.getElementById(elId);
  if (!el || !data.length) return;
  const mx = Math.max(...data, 1);
  el.innerHTML = data.slice(-20).map(v =>
    '<div class="bar" style="height:' + Math.max(2, (v / mx) * 24) + 'px"></div>'
  ).join('');
}

// ─── Test Actions ────────────────────────────────────────────────
async function startTest() {
  const host = document.getElementById('f-host').value;
  if (!host) { toast('Remote host is required', 'error'); return; }
  try {
    const d = await api('POST', '/tests/start', {
      name: document.getElementById('f-name').value,
      scenario: document.getElementById('f-scenario').value,
      remote_host: host,
      remote_port: parseInt(document.getElementById('f-port').value),
      transport: document.getElementById('f-transport').value,
      call_rate: parseFloat(document.getElementById('f-rate').value),
      call_limit: parseInt(document.getElementById('f-limit').value),
      max_calls: parseInt(document.getElementById('f-max').value),
      duration: parseInt(document.getElementById('f-dur').value),
      local_ip: document.getElementById('f-lip').value,
      auth_user: document.getElementById('f-au').value,
      auth_pass: document.getElementById('f-ap').value,
    });
    toast('Test "' + d.id + '" started successfully', 'success');
    clog('Test <b>' + d.id + '</b> started', 'log-info');
    closeModals();
    navigate('dashboard');
  } catch(e) {
    toast(e.message, 'error');
    clog('ERROR: ' + e.message, 'log-err');
  }
}

async function stopTest(id) {
  try {
    await api('POST', '/tests/' + id + '/stop');
    toast('Stopped: ' + id, 'success');
    clog('Stopped: ' + id, 'log-info');
  } catch(e) { toast(e.message, 'error'); }
}

async function removeTest(id) {
  try {
    await api('DELETE', '/tests/' + id);
    toast('Removed: ' + id, 'info');
    clog('Removed: ' + id, 'log-info');
  } catch(e) { toast(e.message, 'error'); }
}

async function stopAllTests() {
  try {
    await api('POST', '/tests/stop-all');
    toast('All tests stopped', 'warn');
    clog('All tests stopped', 'log-warn');
  } catch(e) { toast(e.message, 'error'); }
}

// ─── Connectors ──────────────────────────────────────────────────
async function createConnector() {
  try {
    await api('POST', '/connectors', {
      name: document.getElementById('fc-name').value,
      local_ip: document.getElementById('fc-lip').value,
      local_port: parseInt(document.getElementById('fc-lp').value),
      remote_ip: document.getElementById('fc-rip').value,
      remote_port: parseInt(document.getElementById('fc-rp').value),
      transport: document.getElementById('fc-tr').value,
    });
    toast('Connector created', 'success');
    closeModals();
    loadConnectors();
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteConnector(name) {
  showConfirm('Delete Connector', 'Delete connector "' + name + '"?', async () => {
    try { await api('DELETE', '/connectors/' + name); loadConnectors(); toast('Deleted', 'info'); }
    catch(e) { toast(e.message, 'error'); }
  });
}

async function loadConnectors() {
  try {
    const d = await api('GET', '/connectors');
    const el = document.getElementById('connTable');
    if (!d.connectors.length) {
      el.innerHTML = '<tr><td colspan="8" class="empty-row"><div><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="5" r="3"/><circle cx="5" cy="19" r="3"/><circle cx="19" cy="19" r="3"/><path d="M12 8v2m-5 6l4-6m6 6l-4-6"/></svg></div>No connectors configured</td></tr>';
      return;
    }
    el.innerHTML = d.connectors.map(c => '<tr>' +
      '<td><b>' + c.name + '</b></td><td>' + c.local_ip + '</td><td>' + c.local_port + '</td>' +
      '<td>' + c.remote_ip + '</td><td>' + c.remote_port + '</td>' +
      '<td><span class="badge badge-idle">' + c.transport.toUpperCase() + '</span></td>' +
      '<td>' + (c.enabled ? '<span class="badge badge-run">Enabled</span>' : '<span class="badge badge-stop">Disabled</span>') + '</td>' +
      '<td><button class="btn btn-sm btn-outline" onclick="deleteConnector(\\'' + c.name + '\\')">Delete</button></td>' +
    '</tr>').join('');
  } catch(e) {}
}

// ─── Scenarios ───────────────────────────────────────────────────
async function loadScenarios() {
  try {
    const d = await api('GET', '/scenarios');
    document.getElementById('scenTable').innerHTML = d.scenarios.map(s => '<tr>' +
      '<td><b>' + s.name + '</b></td>' +
      '<td><span class="badge ' + (s.type==='builtin'?'badge-idle':'badge-run') + '">' + s.type + '</span></td>' +
      '<td style="color:var(--text2)">' + s.description + '</td>' +
      '<td><div class="btn-group">' +
        '<button class="btn btn-sm btn-outline" onclick="viewScenario(\\'' + s.name + '\\')">View</button>' +
        (s.type==='custom' ? '<button class="btn btn-sm btn-danger" onclick="deleteScenario(\\'' + s.name + '\\')">Delete</button>' : '') +
      '</div></td></tr>'
    ).join('');
  } catch(e) {}
}

async function viewScenario(name) {
  try {
    const d = await api('GET', '/scenarios/' + name);
    document.getElementById('vs-title').textContent = name;
    document.getElementById('vs-content').textContent = d.content;
    openModal('viewScenario');
  } catch(e) { toast(e.message, 'error'); }
}

async function createScenario() {
  try {
    await api('POST', '/scenarios', {
      name: document.getElementById('fs-name').value,
      description: document.getElementById('fs-desc').value,
      xml_content: document.getElementById('fs-xml').value,
      mode: document.getElementById('fs-mode').value,
    });
    toast('Scenario saved', 'success');
    closeModals();
    loadScenarios();
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteScenario(name) {
  showConfirm('Delete Scenario', 'Delete scenario "' + name + '"?', async () => {
    try { await api('DELETE', '/scenarios/' + name); loadScenarios(); toast('Deleted', 'info'); }
    catch(e) { toast(e.message, 'error'); }
  });
}

// ─── Scenario Selects ────────────────────────────────────────────
async function loadScenarioSelects() {
  try {
    const d = await api('GET', '/scenarios');
    const opts = d.scenarios.map(s => '<option value="' + s.name + '">' + s.name + '</option>').join('');
    ['f-scenario','fh-scenario'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = opts;
    });
  } catch(e) {}
}

// ─── History ─────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const d = await api('GET', '/history');
    const el = document.getElementById('histTable');
    if (!d.history.length) {
      el.innerHTML = '<tr><td colspan="8" class="empty-row"><div><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 8v4l3 3"/><circle cx="12" cy="12" r="10"/></svg></div>No test history yet</td></tr>';
      return;
    }
    el.innerHTML = d.history.map(h => '<tr>' +
      '<td><b>' + h.name + '</b></td><td>' + h.scenario_name + '</td><td>' + badge(h.status) + '</td>' +
      '<td>' + h.total_calls + '</td><td style="color:var(--success)">' + h.successful_calls + '</td>' +
      '<td style="color:var(--danger)">' + h.failed_calls + '</td>' +
      '<td>' + h.call_rate + ' cps</td><td style="color:var(--text2)">' + (h.started_at||'-') + '</td>' +
    '</tr>').join('');
  } catch(e) {}
}

// ─── Config ──────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const d = await api('GET', '/health');
    document.getElementById('c-ver').textContent = d.version;
    document.getElementById('c-stat').innerHTML = '<span class="badge badge-run">' + d.status + '</span>';
    document.getElementById('c-tests').textContent = d.active_tests;
  } catch(e) {}
}

// ─── Main Refresh Loop ──────────────────────────────────────────
async function refresh() {
  try {
    const [stats, tests] = await Promise.all([api('GET', '/stats'), api('GET', '/tests')]);
    const s = stats;

    document.getElementById('s-active').textContent = s.active_instances;
    document.getElementById('s-cps').textContent = s.calls_per_second.toFixed(2);
    document.getElementById('s-total').textContent = s.total_calls.toLocaleString();
    document.getElementById('s-ok').textContent = s.successful_calls.toLocaleString();
    document.getElementById('s-fail').textContent = s.failed_calls.toLocaleString();
    document.getElementById('s-cur').textContent = s.current_calls;
    document.getElementById('s-rt').textContent = Math.round(s.avg_response_time_ms) + 'ms';

    const sr = s.success_rate;
    const srEl = document.getElementById('s-sr');
    srEl.textContent = sr.toFixed(1) + '%';
    srEl.className = 'val ' + (sr >= 95 ? 'g' : sr >= 80 ? 'o' : 'r');

    cpsH.push(s.calls_per_second); if (cpsH.length > 80) cpsH.shift();
    srH.push(sr); if (srH.length > 80) srH.shift();
    sparkCps.push(s.calls_per_second); if (sparkCps.length > 20) sparkCps.shift();

    drawChart('cpsChart', cpsH, 'rgb(0,212,170)', 0.15);
    renderSpark('spark-cps', sparkCps);

    if (currentPage === 'performance') {
      drawChart('perfChart', cpsH, 'rgb(0,212,170)', 0.12);
      drawChart('srChart', srH, 'rgb(124,92,252)', 0.12);
      document.getElementById('p-peak').textContent = Math.max(...cpsH).toFixed(1);
      document.getElementById('p-asr').textContent = (srH.reduce((a,b)=>a+b,0)/srH.length).toFixed(1) + '%';
      document.getElementById('p-art').textContent = Math.round(s.avg_response_time_ms) + 'ms';
    }

    // Tests table
    const tb = document.getElementById('dashTests');
    const ct = document.getElementById('campTable');
    const rows = tests.tests.map(t => {
      const st = t.stats;
      const sc = t.scenario_file.split('/').pop().replace('.xml','');
      return '<tr>' +
        '<td><b>' + t.id + '</b></td><td>' + sc + '</td>' +
        '<td style="font-variant-numeric:tabular-nums">' + t.remote_host + ':' + t.remote_port + '</td>' +
        '<td>' + t.call_rate + '</td><td>' + st.current_calls + '</td><td>' + st.total_calls + '</td>' +
        '<td style="color:var(--success)">' + st.successful_calls + '</td>' +
        '<td style="color:var(--danger)">' + st.failed_calls + '</td>' +
        '<td>' + badge(t.state) + '</td>' +
        '<td><div class="btn-group">' +
          (t.state==='running'
            ? '<button class="btn btn-sm btn-danger" onclick="stopTest(\\'' + t.id + '\\')">Stop</button>'
            : '<button class="btn btn-sm btn-outline" onclick="removeTest(\\'' + t.id + '\\')">Remove</button>'
          ) +
        '</div></td></tr>';
    }).join('');

    const empty = '<tr><td colspan="10" class="empty-row"><div><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div>No active tests</td></tr>';
    tb.innerHTML = rows || empty;
    ct.innerHTML = rows || empty;

    document.getElementById('statusDot').style.background = 'var(--success)';
    document.getElementById('statusText').textContent = 'Connected';
  } catch(e) {
    document.getElementById('statusDot').style.background = 'var(--danger)';
    document.getElementById('statusDot').style.animation = 'none';
    document.getElementById('statusText').textContent = 'Disconnected';
  }
}

setInterval(refresh, 2000);
refresh();
"""

FULL_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GenCall - SIP Traffic Generator</title>
<style>{CSS}</style>
</head>
<body>
{HTML_BODY}
<script>{JS}</script>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    return FULL_HTML

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_alt():
    return FULL_HTML

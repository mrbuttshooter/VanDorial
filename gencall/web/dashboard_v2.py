"""
GenCall Web Dashboard v2 - Full Management Interface
Multi-page SPA with sidebar navigation.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# Split into parts for maintainability
CSS = """
:root{--bg:#0f1117;--sidebar:#161923;--card:#1a1d27;--border:#2a2d3a;--accent:#00d4aa;--accent2:#7c5cfc;
--danger:#ff4757;--warn:#ffa502;--text:#e4e6eb;--text2:#8b8fa3;--success:#00d4aa;--hover:#1f2233}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);display:flex;min-height:100vh}

/* Sidebar */
.sidebar{width:220px;background:var(--sidebar);border-right:1px solid var(--border);position:fixed;top:0;left:0;
height:100vh;display:flex;flex-direction:column;z-index:100}
.sidebar .logo{padding:20px 16px;border-bottom:1px solid var(--border)}
.sidebar .logo h1{font-size:20px;font-weight:800;background:linear-gradient(135deg,var(--accent),var(--accent2));
-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sidebar .logo span{font-size:11px;color:var(--text2)}
.sidebar nav{flex:1;padding:12px 0;overflow-y:auto}
.sidebar nav a{display:flex;align-items:center;padding:10px 20px;color:var(--text2);text-decoration:none;
font-size:13px;font-weight:500;border-left:3px solid transparent;transition:all .15s}
.sidebar nav a:hover{background:var(--hover);color:var(--text)}
.sidebar nav a.active{color:var(--accent);border-left-color:var(--accent);background:rgba(0,212,170,.06)}
.sidebar nav a svg{width:16px;height:16px;margin-right:10px;opacity:.7}
.sidebar nav a.active svg{opacity:1}
.sidebar .nav-section{padding:16px 20px 6px;font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:var(--text2);opacity:.5}

/* Main */
.main{margin-left:220px;flex:1;min-height:100vh}
.topbar{background:var(--card);border-bottom:1px solid var(--border);padding:14px 28px;display:flex;
align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50}
.topbar h2{font-size:18px;font-weight:600}
.topbar .status{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text2)}
.topbar .dot{width:8px;height:8px;border-radius:50%;background:var(--success)}
.content{padding:24px 28px}

/* Cards */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px}
.card h4{font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:var(--text2);margin-bottom:6px}
.card .val{font-size:28px;font-weight:700}
.card .val.g{color:var(--success)}.card .val.r{color:var(--danger)}.card .val.p{color:var(--accent2)}.card .val.o{color:var(--warn)}

/* Tables */
.tbl-wrap{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.tbl-head{padding:16px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border)}
.tbl-head h3{font-size:15px;font-weight:600}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--text2);
padding:10px 16px;border-bottom:1px solid var(--border);font-weight:600}
td{padding:10px 16px;border-bottom:1px solid var(--border);font-size:13px}
tr:hover{background:rgba(255,255,255,.015)}
tr:last-child td{border-bottom:none}

/* Buttons */
.btn{padding:6px 14px;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s}
.btn-primary{background:var(--accent);color:#000}.btn-primary:hover{background:#00e8ba}
.btn-danger{background:var(--danger);color:#fff}.btn-danger:hover{background:#ff6b7a}
.btn-warn{background:var(--warn);color:#000}
.btn-sm{padding:4px 10px;font-size:11px}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text2)}.btn-outline:hover{border-color:var(--text);color:var(--text)}
.btn-group{display:flex;gap:6px}
.btn-enable{background:#27ae60;color:#fff;font-size:11px;padding:4px 12px;border:none;border-radius:4px;cursor:pointer;font-weight:600}
.btn-disable{background:var(--danger);color:#fff;font-size:11px;padding:4px 12px;border:none;border-radius:4px;cursor:pointer;font-weight:600}

/* Badge */
.badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:600}
.badge-run{background:rgba(0,212,170,.12);color:var(--success)}
.badge-stop{background:rgba(139,143,163,.12);color:var(--text2)}
.badge-err{background:rgba(255,71,87,.12);color:var(--danger)}
.badge-idle{background:rgba(124,92,252,.12);color:var(--accent2)}

/* Chart */
.chart-box{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:20px}
.chart-box h3{font-size:13px;color:var(--text2);margin-bottom:12px;text-transform:uppercase;letter-spacing:.5px}
.chart-area{height:180px;display:flex;align-items:flex-end;gap:2px}
.chart-bar{flex:1;background:var(--accent);border-radius:2px 2px 0 0;min-height:1px;transition:height .3s}

/* Modal */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:200;align-items:center;justify-content:center}
.modal-bg.show{display:flex}
.modal{background:var(--card);border:1px solid var(--border);border-radius:12px;width:560px;max-height:80vh;overflow-y:auto}
.modal-head{padding:18px 24px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
.modal-head h3{font-size:16px}
.modal-close{background:none;border:none;color:var(--text2);font-size:20px;cursor:pointer}
.modal-body{padding:24px}
.modal-foot{padding:14px 24px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:10px}

/* Form */
.fg{margin-bottom:14px}
.fg label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--text2);margin-bottom:4px}
.fg input,.fg select,.fg textarea{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:6px;
color:var(--text);padding:9px 12px;font-size:13px;outline:none}
.fg input:focus,.fg select:focus,.fg textarea:focus{border-color:var(--accent)}
.fg textarea{min-height:120px;font-family:monospace;font-size:12px}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}

/* Console */
.console{background:#0a0c10;border:1px solid var(--border);border-radius:8px;padding:14px;
font-family:'Fira Code',Consolas,monospace;font-size:12px;line-height:1.7;max-height:500px;
overflow-y:auto;color:var(--text2)}
.console .log-info{color:var(--accent)}.console .log-warn{color:var(--warn)}.console .log-err{color:var(--danger)}

/* Page hidden */
.page{display:none}.page.active{display:block}

/* Responsive */
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.form-row{grid-template-columns:1fr}}
@media(max-width:600px){.sidebar{width:60px}.sidebar .logo span,.sidebar nav a span,.sidebar .nav-section{display:none}
.sidebar nav a{justify-content:center;padding:12px}.sidebar nav a svg{margin:0}.main{margin-left:60px}.cards{grid-template-columns:1fr}}
"""

ICONS = {
    "dashboard": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    "campaigns": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
    "scenarios": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/></svg>',
    "connectors": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><path d="M22 6l-10 7L2 6"/></svg>',
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

HTML_BODY = """
<div class="sidebar">
  <div class="logo">
    <h1>GenCall</h1>
    <span>SIP Traffic Generator v2.0</span>
  </div>
  <nav>
    <div class="nav-section">Main</div>
    """ + "".join(f'<a href="#{id}" data-page="{id}" class="nav-link">{icon}<span>{label}</span></a>' for id, label, icon in SIDEBAR_ITEMS[:5]) + """
    <div class="nav-section">Monitor</div>
    """ + "".join(f'<a href="#{id}" data-page="{id}" class="nav-link">{icon}<span>{label}</span></a>' for id, label, icon in SIDEBAR_ITEMS[5:]) + """
  </nav>
</div>

<div class="main">
  <div class="topbar">
    <h2 id="pageTitle">Dashboard</h2>
    <div class="status"><div class="dot" id="statusDot"></div><span id="statusText">Connected</span></div>
  </div>
  <div class="content">

    <!-- ═══ DASHBOARD ═══ -->
    <div class="page active" id="pg-dashboard">
      <div class="cards">
        <div class="card"><h4>Active Tests</h4><div class="val p" id="s-active">0</div></div>
        <div class="card"><h4>Calls / Second</h4><div class="val g" id="s-cps">0.00</div></div>
        <div class="card"><h4>Success Rate</h4><div class="val g" id="s-sr">0%</div></div>
        <div class="card"><h4>Total Calls</h4><div class="val" id="s-total">0</div></div>
      </div>
      <div class="cards" style="grid-template-columns:repeat(4,1fr)">
        <div class="card"><h4>Successful</h4><div class="val g" id="s-ok" style="font-size:20px">0</div></div>
        <div class="card"><h4>Failed</h4><div class="val r" id="s-fail" style="font-size:20px">0</div></div>
        <div class="card"><h4>Current Calls</h4><div class="val o" id="s-cur" style="font-size:20px">0</div></div>
        <div class="card"><h4>Avg Response</h4><div class="val" id="s-rt" style="font-size:20px">0ms</div></div>
      </div>
      <div class="chart-box"><h3>Calls Per Second (Live)</h3><div class="chart-area" id="cpsChart"></div></div>
      <div class="tbl-wrap">
        <div class="tbl-head"><h3>Running Tests</h3></div>
        <table><thead><tr><th>ID</th><th>Scenario</th><th>Target</th><th>Rate</th><th>Active</th><th>Total</th><th>OK</th><th>Fail</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody id="dashTests"></tbody></table>
      </div>
    </div>

    <!-- ═══ TEST CAMPAIGNS ═══ -->
    <div class="page" id="pg-campaigns">
      <div style="margin-bottom:16px"><button class="btn btn-primary" onclick="openModal('newTest')">+ New Test Campaign</button></div>
      <div class="tbl-wrap">
        <div class="tbl-head"><h3>Test Campaigns</h3></div>
        <table><thead><tr><th>Name</th><th>Scenario</th><th>Target</th><th>Rate</th><th>Limit</th><th>Max Calls</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody id="campTable"></tbody></table>
      </div>
    </div>

    <!-- ═══ SCENARIOS ═══ -->
    <div class="page" id="pg-scenarios">
      <div style="margin-bottom:16px"><button class="btn btn-primary" onclick="openModal('newScenario')">+ New Scenario</button></div>
      <div class="tbl-wrap">
        <div class="tbl-head"><h3>SIP Scenarios</h3></div>
        <table><thead><tr><th>Name</th><th>Type</th><th>Description</th><th>Actions</th></tr></thead>
        <tbody id="scenTable"></tbody></table>
      </div>
    </div>

    <!-- ═══ CONNECTORS ═══ -->
    <div class="page" id="pg-connectors">
      <div style="margin-bottom:16px"><button class="btn btn-primary" onclick="openModal('newConnector')">+ New Connector</button></div>
      <div class="tbl-wrap">
        <div class="tbl-head"><h3>SIP Connectors</h3></div>
        <table><thead><tr><th>Name</th><th>Local IP</th><th>Port</th><th>Remote IP</th><th>Port</th><th>Transport</th><th>Enabled</th><th>Actions</th></tr></thead>
        <tbody id="connTable"></tbody></table>
      </div>
    </div>

    <!-- ═══ SCHEDULER ═══ -->
    <div class="page" id="pg-scheduler">
      <div style="margin-bottom:16px"><button class="btn btn-primary" onclick="openModal('newSchedule')">+ New Scheduling</button></div>
      <div class="tbl-wrap">
        <div class="tbl-head"><h3>Scheduled Tests</h3></div>
        <table><thead><tr><th>Name</th><th>Scenario</th><th>Schedule</th><th>Next Run</th><th>Runs</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody id="schedTable"><tr><td colspan="7" style="text-align:center;color:var(--text2)">No scheduled tests. Create one to get started.</td></tr></tbody></table>
      </div>
    </div>

    <!-- ═══ CONSOLE ═══ -->
    <div class="page" id="pg-console">
      <div class="chart-box" style="padding:0;overflow:hidden">
        <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
          <h3 style="margin:0">Live Console</h3>
          <button class="btn btn-sm btn-outline" onclick="clearConsole()">Clear</button>
        </div>
        <div class="console" id="consoleLog" style="border:none;border-radius:0;max-height:600px">
          <div class="log-info">[GenCall] System ready.</div>
        </div>
      </div>
    </div>

    <!-- ═══ HISTORY ═══ -->
    <div class="page" id="pg-history">
      <div class="tbl-wrap">
        <div class="tbl-head"><h3>Test History</h3></div>
        <table><thead><tr><th>Name</th><th>Scenario</th><th>Status</th><th>Total</th><th>Success</th><th>Failed</th><th>Rate</th><th>Started</th></tr></thead>
        <tbody id="histTable"></tbody></table>
      </div>
    </div>

    <!-- ═══ PERFORMANCE ═══ -->
    <div class="page" id="pg-performance">
      <div class="cards" style="grid-template-columns:repeat(3,1fr);margin-bottom:20px">
        <div class="card"><h4>Peak CPS</h4><div class="val g" id="p-peak">0</div></div>
        <div class="card"><h4>Avg Success Rate</h4><div class="val" id="p-asr">0%</div></div>
        <div class="card"><h4>Avg Response Time</h4><div class="val" id="p-art">0ms</div></div>
      </div>
      <div class="chart-box"><h3>CPS Over Time</h3><div class="chart-area" id="perfChart" style="height:220px"></div></div>
      <div class="chart-box"><h3>Success Rate Over Time</h3><div class="chart-area" id="srChart" style="height:220px"></div></div>
    </div>

    <!-- ═══ CONFIGURATION ═══ -->
    <div class="page" id="pg-config">
      <div class="cards" style="grid-template-columns:1fr 1fr">
        <div class="card">
          <h4 style="margin-bottom:12px">Server Info</h4>
          <table style="font-size:13px">
            <tr><td style="color:var(--text2);padding:4px 12px 4px 0">Version</td><td id="c-ver">-</td></tr>
            <tr><td style="color:var(--text2);padding:4px 12px 4px 0">Status</td><td id="c-stat">-</td></tr>
            <tr><td style="color:var(--text2);padding:4px 12px 4px 0">Active Tests</td><td id="c-tests">-</td></tr>
            <tr><td style="color:var(--text2);padding:4px 12px 4px 0">API Docs</td><td><a href="/docs" target="_blank" style="color:var(--accent)">/docs</a></td></tr>
          </table>
        </div>
        <div class="card">
          <h4 style="margin-bottom:12px">Quick Actions</h4>
          <div style="display:flex;flex-direction:column;gap:8px">
            <button class="btn btn-danger" onclick="stopAllTests()" style="width:100%">Stop All Tests</button>
            <button class="btn btn-outline" onclick="navigate('campaigns')" style="width:100%">New Test Campaign</button>
            <button class="btn btn-outline" onclick="navigate('scenarios')" style="width:100%">Manage Scenarios</button>
          </div>
        </div>
      </div>
    </div>

  </div>
</div>

<!-- ═══ MODALS ═══ -->
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
    <div class="modal-foot"><button class="btn btn-outline" onclick="closeModals()">Cancel</button><button class="btn btn-primary" onclick="startTest()">Start Test</button></div>
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
    <div class="modal-foot"><button class="btn btn-outline" onclick="closeModals()">Cancel</button><button class="btn btn-primary" onclick="createConnector()">Create</button></div>
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
    <div class="modal-foot"><button class="btn btn-outline" onclick="closeModals()">Cancel</button><button class="btn btn-primary" onclick="createScenario()">Save</button></div>
  </div>
</div>

<div class="modal-bg" id="modal-viewScenario">
  <div class="modal" style="width:700px">
    <div class="modal-head"><h3 id="vs-title">Scenario</h3><button class="modal-close" onclick="closeModals()">&times;</button></div>
    <div class="modal-body"><pre style="background:var(--bg);padding:14px;border-radius:8px;overflow-x:auto;font-size:12px;line-height:1.6;max-height:500px;overflow-y:auto" id="vs-content"></pre></div>
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
    <div class="modal-foot"><button class="btn btn-outline" onclick="closeModals()">Cancel</button><button class="btn btn-primary" onclick="closeModals()">Create Schedule</button></div>
  </div>
</div>
"""

JS = """
let cpsH=[],srH=[],currentPage='dashboard';
const titles={dashboard:'Dashboard',campaigns:'Test Campaigns',scenarios:'Scenarios',connectors:'Connectors',
scheduler:'Scheduler',console:'Console',history:'History',performance:'Performance',config:'Configuration'};

function navigate(page){
  currentPage=page;
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(a=>a.classList.remove('active'));
  const pg=document.getElementById('pg-'+page);
  if(pg)pg.classList.add('active');
  const nl=document.querySelector(`[data-page="${page}"]`);
  if(nl)nl.classList.add('active');
  document.getElementById('pageTitle').textContent=titles[page]||page;
  if(page==='scenarios')loadScenarios();
  if(page==='connectors')loadConnectors();
  if(page==='history')loadHistory();
  if(page==='config')loadConfig();
}

// Router
window.addEventListener('hashchange',()=>{const h=location.hash.slice(1);if(h)navigate(h);});
window.addEventListener('load',()=>{const h=location.hash.slice(1);navigate(h||'dashboard');loadScenarioSelects();});

document.querySelectorAll('.nav-link').forEach(a=>a.addEventListener('click',e=>{
  e.preventDefault();navigate(a.dataset.page);history.pushState(null,null,'#'+a.dataset.page);
}));

// API helper
async function api(method,path,body){
  const o={method,headers:{'Content-Type':'application/json'}};
  if(body)o.body=JSON.stringify(body);
  const r=await fetch('/api'+path,o);
  if(!r.ok){const e=await r.json().catch(()=>({detail:r.statusText}));throw new Error(e.detail||'Error');}
  return r.json();
}

// Modals
function openModal(id){document.getElementById('modal-'+id).classList.add('show');}
function closeModals(){document.querySelectorAll('.modal-bg').forEach(m=>m.classList.remove('show'));}

// Console
function clog(msg,cls=''){
  const c=document.getElementById('consoleLog');
  const t=new Date().toLocaleTimeString();
  c.innerHTML+=`<div class="${cls}">[${t}] ${msg}</div>`;
  c.scrollTop=c.scrollHeight;
}
function clearConsole(){document.getElementById('consoleLog').innerHTML='';}

// Badge helper
function badge(state){
  const m={running:'badge-run',stopped:'badge-stop',error:'badge-err',idle:'badge-idle',completed:'badge-stop',failed:'badge-err'};
  return `<span class="badge ${m[state]||'badge-idle'}">${state}</span>`;
}

// Chart helper
function renderChart(el,data,maxH){
  if(!data.length)return;
  const mx=Math.max(...data,1);
  el.innerHTML=data.map(v=>`<div class="chart-bar" style="height:${Math.max(1,(v/mx)*maxH)}px"></div>`).join('');
}

// ─── Start Test ──────────────────────────────────────────────────────
async function startTest(){
  const host=document.getElementById('f-host').value;
  if(!host){clog('ERROR: Remote host required','log-err');return;}
  try{
    const d=await api('POST','/tests/start',{
      name:document.getElementById('f-name').value,
      scenario:document.getElementById('f-scenario').value,
      remote_host:host,
      remote_port:parseInt(document.getElementById('f-port').value),
      transport:document.getElementById('f-transport').value,
      call_rate:parseFloat(document.getElementById('f-rate').value),
      call_limit:parseInt(document.getElementById('f-limit').value),
      max_calls:parseInt(document.getElementById('f-max').value),
      duration:parseInt(document.getElementById('f-dur').value),
      local_ip:document.getElementById('f-lip').value,
      auth_user:document.getElementById('f-au').value,
      auth_pass:document.getElementById('f-ap').value,
    });
    clog(`Test <b>${d.id}</b> started`,'log-info');
    closeModals();navigate('dashboard');
  }catch(e){clog('ERROR: '+e.message,'log-err');}
}

async function stopTest(id){try{await api('POST',`/tests/${id}/stop`);clog(`Stopped: ${id}`,'log-info');}catch(e){clog(e.message,'log-err');}}
async function removeTest(id){try{await api('DELETE',`/tests/${id}`);clog(`Removed: ${id}`,'log-info');}catch(e){clog(e.message,'log-err');}}
async function stopAllTests(){try{await api('POST','/tests/stop-all');clog('All tests stopped','log-warn');}catch(e){clog(e.message,'log-err');}}

// ─── Connectors ──────────────────────────────────────────────────────
async function createConnector(){
  try{
    await api('POST','/connectors',{
      name:document.getElementById('fc-name').value,
      local_ip:document.getElementById('fc-lip').value,
      local_port:parseInt(document.getElementById('fc-lp').value),
      remote_ip:document.getElementById('fc-rip').value,
      remote_port:parseInt(document.getElementById('fc-rp').value),
      transport:document.getElementById('fc-tr').value,
    });
    clog('Connector created','log-info');closeModals();loadConnectors();
  }catch(e){clog(e.message,'log-err');}
}
async function deleteConnector(name){
  if(!confirm('Delete connector '+name+'?'))return;
  try{await api('DELETE','/connectors/'+name);loadConnectors();}catch(e){clog(e.message,'log-err');}
}
async function loadConnectors(){
  try{
    const d=await api('GET','/connectors');
    document.getElementById('connTable').innerHTML=d.connectors.length?d.connectors.map(c=>`<tr>
      <td><b>${c.name}</b></td><td>${c.local_ip}</td><td>${c.local_port}</td><td>${c.remote_ip}</td><td>${c.remote_port}</td>
      <td>${c.transport.toUpperCase()}</td><td>${c.enabled?'<button class="btn-enable">Enabled</button>':'<button class="btn-disable">Disabled</button>'}</td>
      <td><div class="btn-group"><button class="btn btn-sm btn-outline" onclick="deleteConnector('${c.name}')">Delete</button></div></td>
    </tr>`).join(''):'<tr><td colspan="8" style="text-align:center;color:var(--text2)">No connectors</td></tr>';
  }catch(e){}
}

// ─── Scenarios ───────────────────────────────────────────────────────
async function loadScenarios(){
  try{
    const d=await api('GET','/scenarios');
    document.getElementById('scenTable').innerHTML=d.scenarios.map(s=>`<tr>
      <td><b>${s.name}</b></td><td><span class="badge ${s.type==='builtin'?'badge-idle':'badge-run'}">${s.type}</span></td>
      <td>${s.description}</td>
      <td><div class="btn-group">
        <button class="btn btn-sm btn-outline" onclick="viewScenario('${s.name}')">View</button>
        ${s.type==='custom'?`<button class="btn btn-sm btn-danger" onclick="deleteScenario('${s.name}')">Delete</button>`:''}
      </div></td>
    </tr>`).join('');
  }catch(e){}
}
async function viewScenario(name){
  try{
    const d=await api('GET','/scenarios/'+name);
    document.getElementById('vs-title').textContent=name;
    document.getElementById('vs-content').textContent=d.content;
    openModal('viewScenario');
  }catch(e){clog(e.message,'log-err');}
}
async function createScenario(){
  try{
    await api('POST','/scenarios',{
      name:document.getElementById('fs-name').value,
      description:document.getElementById('fs-desc').value,
      xml_content:document.getElementById('fs-xml').value,
      mode:document.getElementById('fs-mode').value,
    });
    clog('Scenario saved','log-info');closeModals();loadScenarios();
  }catch(e){clog(e.message,'log-err');}
}
async function deleteScenario(name){
  if(!confirm('Delete scenario '+name+'?'))return;
  try{await api('DELETE','/scenarios/'+name);loadScenarios();}catch(e){clog(e.message,'log-err');}
}

// ─── Scenario Selects ────────────────────────────────────────────────
async function loadScenarioSelects(){
  try{
    const d=await api('GET','/scenarios');
    const opts=d.scenarios.map(s=>`<option value="${s.name}">${s.name}</option>`).join('');
    ['f-scenario','fh-scenario'].forEach(id=>{const el=document.getElementById(id);if(el)el.innerHTML=opts;});
  }catch(e){}
}

// ─── History ─────────────────────────────────────────────────────────
async function loadHistory(){
  try{
    const d=await api('GET','/history');
    document.getElementById('histTable').innerHTML=d.history.length?d.history.map(h=>`<tr>
      <td><b>${h.name}</b></td><td>${h.scenario_name}</td><td>${badge(h.status)}</td>
      <td>${h.total_calls}</td><td style="color:var(--success)">${h.successful_calls}</td><td style="color:var(--danger)">${h.failed_calls}</td>
      <td>${h.call_rate} cps</td><td>${h.started_at||'-'}</td>
    </tr>`).join(''):'<tr><td colspan="8" style="text-align:center;color:var(--text2)">No test history yet</td></tr>';
  }catch(e){}
}

// ─── Config ──────────────────────────────────────────────────────────
async function loadConfig(){
  try{
    const d=await api('GET','/health');
    document.getElementById('c-ver').textContent=d.version;
    document.getElementById('c-stat').textContent=d.status;
    document.getElementById('c-tests').textContent=d.active_tests;
  }catch(e){}
}

// ─── Main refresh loop ──────────────────────────────────────────────
async function refresh(){
  try{
    const[stats,tests]=await Promise.all([api('GET','/stats'),api('GET','/tests')]);
    const s=stats;
    document.getElementById('s-active').textContent=s.active_instances;
    document.getElementById('s-cps').textContent=s.calls_per_second.toFixed(2);
    document.getElementById('s-total').textContent=s.total_calls.toLocaleString();
    document.getElementById('s-ok').textContent=s.successful_calls.toLocaleString();
    document.getElementById('s-fail').textContent=s.failed_calls.toLocaleString();
    document.getElementById('s-cur').textContent=s.current_calls;
    document.getElementById('s-rt').textContent=Math.round(s.avg_response_time_ms)+'ms';
    const sr=s.success_rate;
    const srEl=document.getElementById('s-sr');
    srEl.textContent=sr.toFixed(1)+'%';
    srEl.className='val '+(sr>=95?'g':sr>=80?'o':'r');

    cpsH.push(s.calls_per_second);if(cpsH.length>80)cpsH.shift();
    srH.push(sr);if(srH.length>80)srH.shift();
    renderChart(document.getElementById('cpsChart'),cpsH,170);

    if(currentPage==='performance'){
      renderChart(document.getElementById('perfChart'),cpsH,210);
      renderChart(document.getElementById('srChart'),srH,210);
      document.getElementById('p-peak').textContent=Math.max(...cpsH).toFixed(1);
      document.getElementById('p-asr').textContent=(srH.reduce((a,b)=>a+b,0)/srH.length).toFixed(1)+'%';
      document.getElementById('p-art').textContent=Math.round(s.avg_response_time_ms)+'ms';
    }

    // Tests table
    const tb=document.getElementById('dashTests');
    const ct=document.getElementById('campTable');
    const rows=tests.tests.map(t=>{
      const st=t.stats;const sc=t.scenario_file.split('/').pop().replace('.xml','');
      return `<tr><td><b>${t.id}</b></td><td>${sc}</td><td>${t.remote_host}:${t.remote_port}</td>
        <td>${t.call_rate}</td><td>${st.current_calls}</td><td>${st.total_calls}</td>
        <td style="color:var(--success)">${st.successful_calls}</td><td style="color:var(--danger)">${st.failed_calls}</td>
        <td>${badge(t.state)}</td>
        <td><div class="btn-group">${t.state==='running'
          ?`<button class="btn btn-sm btn-danger" onclick="stopTest('${t.id}')">Stop</button>`
          :`<button class="btn btn-sm btn-outline" onclick="removeTest('${t.id}')">Remove</button>`
        }</div></td></tr>`;
    }).join('');
    const empty='<tr><td colspan="10" style="text-align:center;color:var(--text2)">No active tests</td></tr>';
    tb.innerHTML=rows||empty;
    ct.innerHTML=rows||empty;

    document.getElementById('statusDot').style.background='var(--success)';
    document.getElementById('statusText').textContent='Connected';
  }catch(e){
    document.getElementById('statusDot').style.background='var(--danger)';
    document.getElementById('statusText').textContent='Disconnected';
  }
}
setInterval(refresh,2000);refresh();
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

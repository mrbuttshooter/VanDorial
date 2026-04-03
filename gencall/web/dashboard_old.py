"""
GenCall Web Dashboard.
Serves the single-page HTML dashboard for the traffic generator.
"""

import os
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GenCall - SIP Traffic Generator</title>
<style>
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3a;
    --accent: #00d4aa;
    --accent2: #7c5cfc;
    --danger: #ff4757;
    --warning: #ffa502;
    --text: #e4e6eb;
    --text2: #8b8fa3;
    --success: #00d4aa;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }

  /* Header */
  .header {
    background: var(--card);
    border-bottom: 1px solid var(--border);
    padding: 16px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .header h1 {
    font-size: 24px;
    font-weight: 700;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .header .version { color: var(--text2); font-size: 13px; }
  .header .status-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--success); display: inline-block; margin-right: 8px;
  }

  /* Layout */
  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .grid { display: grid; gap: 20px; }
  .grid-4 { grid-template-columns: repeat(4, 1fr); }
  .grid-2 { grid-template-columns: repeat(2, 1fr); }

  /* Cards */
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
  }
  .card h3 { font-size: 13px; color: var(--text2); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  .card .value { font-size: 32px; font-weight: 700; }
  .card .value.green { color: var(--success); }
  .card .value.red { color: var(--danger); }
  .card .value.purple { color: var(--accent2); }
  .card .value.orange { color: var(--warning); }

  /* Stats Grid */
  .stats-row { margin-bottom: 24px; }

  /* Test Control Panel */
  .control-panel { margin-bottom: 24px; }
  .control-panel h2 { font-size: 18px; margin-bottom: 16px; }
  .form-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  .form-group { display: flex; flex-direction: column; }
  .form-group label { font-size: 12px; color: var(--text2); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
  .form-group input, .form-group select {
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    color: var(--text); padding: 10px 12px; font-size: 14px; outline: none;
  }
  .form-group input:focus, .form-group select:focus { border-color: var(--accent); }

  /* Buttons */
  .btn {
    padding: 10px 24px; border: none; border-radius: 8px; cursor: pointer;
    font-size: 14px; font-weight: 600; transition: all 0.2s;
  }
  .btn-primary { background: var(--accent); color: #000; }
  .btn-primary:hover { background: #00e8ba; }
  .btn-danger { background: var(--danger); color: #fff; }
  .btn-danger:hover { background: #ff6b7a; }
  .btn-secondary { background: var(--border); color: var(--text); }
  .btn-row { display: flex; gap: 12px; margin-top: 16px; }

  /* Test List */
  .test-list { margin-top: 24px; }
  .test-list h2 { font-size: 18px; margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 12px; color: var(--text2); text-transform: uppercase;
       letter-spacing: 0.5px; padding: 12px; border-bottom: 1px solid var(--border); }
  td { padding: 12px; border-bottom: 1px solid var(--border); font-size: 14px; }
  tr:hover { background: rgba(255,255,255,0.02); }

  .badge {
    display: inline-block; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600;
  }
  .badge-running { background: rgba(0,212,170,0.15); color: var(--success); }
  .badge-stopped { background: rgba(139,143,163,0.15); color: var(--text2); }
  .badge-error { background: rgba(255,71,87,0.15); color: var(--danger); }
  .badge-idle { background: rgba(124,92,252,0.15); color: var(--accent2); }

  /* Chart area */
  .chart-area {
    height: 200px; display: flex; align-items: flex-end; gap: 2px;
    padding: 8px 0; border-bottom: 1px solid var(--border);
  }
  .chart-bar {
    flex: 1; background: var(--accent); border-radius: 2px 2px 0 0;
    min-height: 2px; transition: height 0.3s;
  }

  /* Responsive */
  @media (max-width: 900px) {
    .grid-4 { grid-template-columns: repeat(2, 1fr); }
    .form-grid { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 600px) {
    .grid-4 { grid-template-columns: 1fr; }
    .grid-2 { grid-template-columns: 1fr; }
    .form-grid { grid-template-columns: 1fr; }
  }

  /* Scenario selector */
  .scenario-list { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
  .scenario-chip {
    padding: 6px 14px; border-radius: 20px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text2); cursor: pointer; font-size: 13px;
    transition: all 0.2s;
  }
  .scenario-chip:hover, .scenario-chip.active {
    border-color: var(--accent); color: var(--accent); background: rgba(0,212,170,0.08);
  }

  /* Log area */
  .log-area {
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; font-family: 'Fira Code', 'Consolas', monospace; font-size: 13px;
    max-height: 200px; overflow-y: auto; color: var(--text2); line-height: 1.6;
  }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>GenCall</h1>
    <span class="version">SIP Traffic Generator v2.0</span>
  </div>
  <div>
    <span class="status-dot" id="statusDot"></span>
    <span id="statusText">Connected</span>
  </div>
</div>

<div class="container">

  <!-- Stats Row -->
  <div class="stats-row grid grid-4">
    <div class="card">
      <h3>Active Tests</h3>
      <div class="value purple" id="activeTests">0</div>
    </div>
    <div class="card">
      <h3>Calls/Second</h3>
      <div class="value green" id="cps">0.00</div>
    </div>
    <div class="card">
      <h3>Success Rate</h3>
      <div class="value green" id="successRate">0%</div>
    </div>
    <div class="card">
      <h3>Total Calls</h3>
      <div class="value" id="totalCalls">0</div>
    </div>
  </div>

  <!-- CPS Chart -->
  <div class="card" style="margin-bottom: 24px;">
    <h3>Calls Per Second (Live)</h3>
    <div class="chart-area" id="cpsChart"></div>
  </div>

  <!-- Control Panel -->
  <div class="card control-panel">
    <h2>New Test</h2>

    <div style="margin-bottom: 16px;">
      <h3 style="margin-bottom: 8px;">Scenario</h3>
      <div class="scenario-list" id="scenarioList">
        <span class="scenario-chip active" data-name="basic_call">Basic Call</span>
        <span class="scenario-chip" data-name="basic_register">Register</span>
        <span class="scenario-chip" data-name="call_with_auth">Call + Auth</span>
        <span class="scenario-chip" data-name="uas_answer">UAS Answer</span>
        <span class="scenario-chip" data-name="options_ping">OPTIONS Ping</span>
        <span class="scenario-chip" data-name="stress_test">Stress Test</span>
      </div>
    </div>

    <div class="form-grid">
      <div class="form-group">
        <label>Test Name</label>
        <input type="text" id="testName" placeholder="my-test-01">
      </div>
      <div class="form-group">
        <label>Remote Host (IP)</label>
        <input type="text" id="remoteHost" placeholder="10.0.0.1">
      </div>
      <div class="form-group">
        <label>Remote Port</label>
        <input type="number" id="remotePort" value="5060">
      </div>
      <div class="form-group">
        <label>Transport</label>
        <select id="transport">
          <option value="udp">UDP</option>
          <option value="tcp">TCP</option>
          <option value="tls">TLS</option>
        </select>
      </div>
      <div class="form-group">
        <label>Local IP (optional)</label>
        <input type="text" id="localIp" placeholder="auto-detect">
      </div>
      <div class="form-group">
        <label>Local Port</label>
        <input type="number" id="localPort" value="5060">
      </div>
      <div class="form-group">
        <label>Call Rate (cps)</label>
        <input type="number" id="callRate" value="1" step="0.1" min="0.1">
      </div>
      <div class="form-group">
        <label>Concurrent Calls</label>
        <input type="number" id="callLimit" value="10" min="1">
      </div>
      <div class="form-group">
        <label>Max Calls (0=unlimited)</label>
        <input type="number" id="maxCalls" value="0" min="0">
      </div>
      <div class="form-group">
        <label>Duration (sec, 0=forever)</label>
        <input type="number" id="duration" value="0" min="0">
      </div>
      <div class="form-group">
        <label>Auth Username</label>
        <input type="text" id="authUser" placeholder="">
      </div>
      <div class="form-group">
        <label>Auth Password</label>
        <input type="password" id="authPass" placeholder="">
      </div>
    </div>

    <div class="btn-row">
      <button class="btn btn-primary" onclick="startTest()">Start Test</button>
      <button class="btn btn-danger" onclick="stopAll()">Stop All</button>
    </div>
  </div>

  <!-- Active Tests Table -->
  <div class="card test-list">
    <h2>Active Tests</h2>
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Scenario</th>
          <th>Target</th>
          <th>Rate</th>
          <th>Current</th>
          <th>Total</th>
          <th>Success</th>
          <th>Failed</th>
          <th>Status</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="testTable"></tbody>
    </table>
  </div>

  <!-- Log Area -->
  <div class="card" style="margin-top: 24px;">
    <h3>Activity Log</h3>
    <div class="log-area" id="logArea">
      <div>[GenCall] System ready. Configure your test and hit Start.</div>
    </div>
  </div>

</div>

<script>
  let selectedScenario = 'basic_call';
  let cpsHistory = [];
  const MAX_CHART_POINTS = 100;

  // Scenario selection
  document.querySelectorAll('.scenario-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.scenario-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      selectedScenario = chip.dataset.name;
    });
  });

  function log(msg) {
    const area = document.getElementById('logArea');
    const ts = new Date().toLocaleTimeString();
    area.innerHTML += `<div>[${ts}] ${msg}</div>`;
    area.scrollTop = area.scrollHeight;
  }

  async function api(method, path, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch('/api' + path, opts);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'API Error');
    }
    return res.json();
  }

  async function startTest() {
    const host = document.getElementById('remoteHost').value;
    if (!host) { log('ERROR: Remote host is required'); return; }

    try {
      const data = await api('POST', '/tests/start', {
        name: document.getElementById('testName').value,
        scenario: selectedScenario,
        remote_host: host,
        remote_port: parseInt(document.getElementById('remotePort').value),
        local_ip: document.getElementById('localIp').value,
        local_port: parseInt(document.getElementById('localPort').value),
        transport: document.getElementById('transport').value,
        call_rate: parseFloat(document.getElementById('callRate').value),
        call_limit: parseInt(document.getElementById('callLimit').value),
        max_calls: parseInt(document.getElementById('maxCalls').value),
        duration: parseInt(document.getElementById('duration').value),
        auth_user: document.getElementById('authUser').value,
        auth_pass: document.getElementById('authPass').value,
      });
      log(`Test <b>${data.id}</b> started with scenario: ${selectedScenario}`);
    } catch (e) {
      log(`ERROR: ${e.message}`);
    }
  }

  async function stopTest(id) {
    try {
      await api('POST', `/tests/${id}/stop`);
      log(`Test <b>${id}</b> stopped`);
    } catch (e) {
      log(`ERROR: ${e.message}`);
    }
  }

  async function removeTest(id) {
    try {
      await api('DELETE', `/tests/${id}`);
      log(`Test <b>${id}</b> removed`);
    } catch (e) {
      log(`ERROR: ${e.message}`);
    }
  }

  async function stopAll() {
    try {
      await api('POST', '/tests/stop-all');
      log('All tests stopped');
    } catch (e) {
      log(`ERROR: ${e.message}`);
    }
  }

  function badgeClass(state) {
    if (state === 'running') return 'badge-running';
    if (state === 'error') return 'badge-error';
    if (state === 'idle' || state === 'starting') return 'badge-idle';
    return 'badge-stopped';
  }

  function updateChart(cps) {
    cpsHistory.push(cps);
    if (cpsHistory.length > MAX_CHART_POINTS) cpsHistory.shift();
    const max = Math.max(...cpsHistory, 1);
    const chart = document.getElementById('cpsChart');
    chart.innerHTML = cpsHistory.map(v =>
      `<div class="chart-bar" style="height:${Math.max(2, (v/max)*180)}px"></div>`
    ).join('');
  }

  async function refresh() {
    try {
      const [statsData, testsData] = await Promise.all([
        api('GET', '/stats'),
        api('GET', '/tests'),
      ]);

      document.getElementById('activeTests').textContent = statsData.active_instances;
      document.getElementById('cps').textContent = statsData.calls_per_second.toFixed(2);
      document.getElementById('totalCalls').textContent = statsData.total_calls.toLocaleString();

      const sr = statsData.success_rate;
      const srEl = document.getElementById('successRate');
      srEl.textContent = sr.toFixed(1) + '%';
      srEl.className = 'value ' + (sr >= 95 ? 'green' : sr >= 80 ? 'orange' : 'red');

      updateChart(statsData.calls_per_second);

      const tbody = document.getElementById('testTable');
      tbody.innerHTML = testsData.tests.map(t => `
        <tr>
          <td>${t.id}</td>
          <td>${t.scenario_file.split('/').pop().replace('.xml','')}</td>
          <td>${t.remote_host}:${t.remote_port}</td>
          <td>${t.call_rate}</td>
          <td>${t.stats.current_calls}</td>
          <td>${t.stats.total_calls}</td>
          <td style="color:var(--success)">${t.stats.successful_calls}</td>
          <td style="color:var(--danger)">${t.stats.failed_calls}</td>
          <td><span class="badge ${badgeClass(t.state)}">${t.state}</span></td>
          <td>
            ${t.state === 'running'
              ? `<button class="btn btn-danger" style="padding:4px 12px;font-size:12px" onclick="stopTest('${t.id}')">Stop</button>`
              : `<button class="btn btn-secondary" style="padding:4px 12px;font-size:12px" onclick="removeTest('${t.id}')">Remove</button>`
            }
          </td>
        </tr>
      `).join('');

      document.getElementById('statusDot').style.background = 'var(--success)';
      document.getElementById('statusText').textContent = 'Connected';
    } catch (e) {
      document.getElementById('statusDot').style.background = 'var(--danger)';
      document.getElementById('statusText').textContent = 'Disconnected';
    }
  }

  // Poll every 2 seconds
  setInterval(refresh, 2000);
  refresh();
</script>

</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_alt():
    return DASHBOARD_HTML

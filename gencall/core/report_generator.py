"""
GenCall HTML Report Generator.

Produces beautiful standalone HTML reports with inline CSS and JavaScript.
No external dependencies - everything is embedded in a single HTML file
that can be emailed, shared, or viewed offline.

Includes:
  - Test summary dashboard
  - CPS over time chart (inline SVG)
  - Success / failure pie chart (inline SVG)
  - CDR table with sorting
  - SIP flow diagrams
  - RTP quality metrics with MOS gauge
  - Alert history
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import math
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("gencall.report_generator")


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class ReportTestSummary:
    """High-level test metadata for the report."""
    test_id: str = ""
    test_name: str = ""
    scenario: str = ""
    target: str = ""
    started_at: Optional[datetime.datetime] = None
    ended_at: Optional[datetime.datetime] = None
    duration_seconds: float = 0.0
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    max_cps: float = 0.0
    avg_cps: float = 0.0
    success_rate: float = 0.0

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "test_name": self.test_name,
            "scenario": self.scenario,
            "target": self.target,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "max_cps": round(self.max_cps, 2),
            "avg_cps": round(self.avg_cps, 2),
            "success_rate": round(self.success_rate, 2),
        }


@dataclass
class CPSDataPoint:
    """A single CPS measurement over time."""
    timestamp: float = 0.0
    cps: float = 0.0
    success_rate: float = 0.0

    def to_dict(self) -> dict:
        return {
            "timestamp": round(self.timestamp, 1),
            "cps": round(self.cps, 2),
            "success_rate": round(self.success_rate, 2),
        }


@dataclass
class CDREntry:
    """CDR entry for the report table."""
    call_id: str = ""
    caller: str = ""
    callee: str = ""
    start_time: str = ""
    duration: float = 0.0
    status: str = ""
    sip_code: int = 0
    codec: str = ""
    mos: float = 0.0

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "caller": self.caller,
            "callee": self.callee,
            "start_time": self.start_time,
            "duration": round(self.duration, 3),
            "status": self.status,
            "sip_code": self.sip_code,
            "codec": self.codec,
            "mos": round(self.mos, 2),
        }


@dataclass
class SIPFlowArrow:
    """Arrow in a SIP flow diagram."""
    timestamp: str = ""
    source: str = ""
    destination: str = ""
    label: str = ""
    is_request: bool = True

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "destination": self.destination,
            "label": self.label,
            "is_request": self.is_request,
        }


@dataclass
class RTPMetrics:
    """RTP quality metrics for the report."""
    codec: str = ""
    packets_sent: int = 0
    packets_received: int = 0
    packets_lost: int = 0
    packet_loss_pct: float = 0.0
    jitter_ms: float = 0.0
    avg_latency_ms: float = 0.0
    r_factor: float = 0.0
    mos: float = 0.0

    def to_dict(self) -> dict:
        return {
            "codec": self.codec,
            "packets_sent": self.packets_sent,
            "packets_received": self.packets_received,
            "packets_lost": self.packets_lost,
            "packet_loss_pct": round(self.packet_loss_pct, 2),
            "jitter_ms": round(self.jitter_ms, 2),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "r_factor": round(self.r_factor, 1),
            "mos": round(self.mos, 2),
        }


@dataclass
class AlertEntry:
    """Alert for the report."""
    timestamp: str = ""
    severity: str = "info"
    rule_name: str = ""
    message: str = ""
    state: str = "firing"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "severity": self.severity,
            "rule_name": self.rule_name,
            "message": self.message,
            "state": self.state,
        }


@dataclass
class ReportData:
    """All data needed to generate a report."""
    report_id: str = ""
    title: str = "GenCall Test Report"
    summary: Optional[ReportTestSummary] = None
    cps_timeline: list[CPSDataPoint] = field(default_factory=list)
    cdr_entries: list[CDREntry] = field(default_factory=list)
    sip_flows: list[SIPFlowArrow] = field(default_factory=list)
    rtp_metrics: Optional[RTPMetrics] = None
    alerts: list[AlertEntry] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.report_id:
            self.report_id = uuid.uuid4().hex[:12]
        if self.summary is None:
            self.summary = ReportTestSummary()

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "title": self.title,
            "summary": self.summary.to_dict() if self.summary else None,
            "cps_timeline": [p.to_dict() for p in self.cps_timeline],
            "cdr_entries": [c.to_dict() for c in self.cdr_entries],
            "sip_flows": [f.to_dict() for f in self.sip_flows],
            "rtp_metrics": self.rtp_metrics.to_dict() if self.rtp_metrics else None,
            "alerts": [a.to_dict() for a in self.alerts],
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# SVG generators
# ---------------------------------------------------------------------------

def _generate_cps_chart_svg(timeline: list[CPSDataPoint], width: int = 800, height: int = 300) -> str:
    """Generate an inline SVG line chart of CPS over time."""
    if not timeline:
        return '<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg"><text x="50%" y="50%" text-anchor="middle" fill="#666">No CPS data available</text></svg>'.format(w=width, h=height)

    margin_l, margin_r, margin_t, margin_b = 60, 20, 20, 40
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    max_cps = max((p.cps for p in timeline), default=1.0) * 1.1
    if max_cps == 0:
        max_cps = 1.0
    min_ts = timeline[0].timestamp
    max_ts = timeline[-1].timestamp
    ts_range = max_ts - min_ts
    if ts_range == 0:
        ts_range = 1.0

    def x_pos(ts: float) -> float:
        return margin_l + ((ts - min_ts) / ts_range) * plot_w

    def y_pos(cps: float) -> float:
        return margin_t + plot_h - (cps / max_cps) * plot_h

    parts: list[str] = []
    parts.append(f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">')
    parts.append(f'<rect width="{width}" height="{height}" fill="#fafbfc" rx="4"/>')

    # Grid lines
    for i in range(5):
        gy = margin_t + (plot_h / 4) * i
        gv = max_cps - (max_cps / 4) * i
        parts.append(f'<line x1="{margin_l}" y1="{gy:.1f}" x2="{width - margin_r}" y2="{gy:.1f}" stroke="#e0e0e0" stroke-width="1"/>')
        parts.append(f'<text x="{margin_l - 5}" y="{gy + 4:.1f}" text-anchor="end" font-size="10" fill="#666">{gv:.0f}</text>')

    # Axes
    parts.append(f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{margin_t + plot_h}" stroke="#333" stroke-width="1"/>')
    parts.append(f'<line x1="{margin_l}" y1="{margin_t + plot_h}" x2="{width - margin_r}" y2="{margin_t + plot_h}" stroke="#333" stroke-width="1"/>')

    # Y-axis label
    parts.append(f'<text x="15" y="{height // 2}" text-anchor="middle" font-size="11" fill="#333" transform="rotate(-90 15 {height // 2})">Calls/sec</text>')

    # X-axis label
    parts.append(f'<text x="{width // 2}" y="{height - 5}" text-anchor="middle" font-size="11" fill="#333">Time (seconds)</text>')

    # X-axis ticks
    num_ticks = min(8, len(timeline))
    for i in range(num_ticks + 1):
        t = min_ts + (ts_range / max(num_ticks, 1)) * i
        tx = x_pos(t)
        parts.append(f'<text x="{tx:.1f}" y="{margin_t + plot_h + 15}" text-anchor="middle" font-size="9" fill="#666">{t - min_ts:.0f}s</text>')

    # CPS line
    points = " ".join(f"{x_pos(p.timestamp):.1f},{y_pos(p.cps):.1f}" for p in timeline)
    parts.append(f'<polyline points="{points}" fill="none" stroke="#2563eb" stroke-width="2" stroke-linejoin="round"/>')

    # Area fill
    area_points = f"{x_pos(timeline[0].timestamp):.1f},{margin_t + plot_h:.1f} {points} {x_pos(timeline[-1].timestamp):.1f},{margin_t + plot_h:.1f}"
    parts.append(f'<polygon points="{area_points}" fill="#2563eb" fill-opacity="0.08"/>')

    parts.append('</svg>')
    return "\n".join(parts)


def _generate_pie_chart_svg(success: int, failure: int, size: int = 200) -> str:
    """Generate an inline SVG pie chart for success/failure ratio."""
    total = success + failure
    if total == 0:
        return f'<svg width="{size}" height="{size}" xmlns="http://www.w3.org/2000/svg"><text x="50%" y="50%" text-anchor="middle" fill="#666">No data</text></svg>'

    cx, cy, r = size // 2, size // 2, size // 2 - 20
    success_pct = success / total
    failure_pct = failure / total

    parts: list[str] = []
    parts.append(f'<svg width="{size}" height="{size + 60}" xmlns="http://www.w3.org/2000/svg">')

    if failure == 0:
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#22c55e"/>')
    elif success == 0:
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#ef4444"/>')
    else:
        # Success arc
        angle = success_pct * 2 * math.pi
        x1, y1 = cx, cy - r
        x2 = cx + r * math.sin(angle)
        y2 = cy - r * math.cos(angle)
        large = 1 if success_pct > 0.5 else 0
        parts.append(f'<path d="M{cx},{cy} L{x1},{y1} A{r},{r} 0 {large},1 {x2:.1f},{y2:.1f} Z" fill="#22c55e"/>')

        # Failure arc
        parts.append(f'<path d="M{cx},{cy} L{x2:.1f},{y2:.1f} A{r},{r} 0 {1 - large},1 {x1},{y1} Z" fill="#ef4444"/>')

    # Center label
    parts.append(f'<text x="{cx}" y="{cy + 5}" text-anchor="middle" font-size="18" font-weight="bold" fill="white">{success_pct * 100:.1f}%</text>')

    # Legend
    ly = size + 10
    parts.append(f'<rect x="20" y="{ly}" width="12" height="12" fill="#22c55e" rx="2"/>')
    parts.append(f'<text x="38" y="{ly + 10}" font-size="11" fill="#333">Success: {success:,}</text>')
    parts.append(f'<rect x="{size // 2 + 10}" y="{ly}" width="12" height="12" fill="#ef4444" rx="2"/>')
    parts.append(f'<text x="{size // 2 + 28}" y="{ly + 10}" font-size="11" fill="#333">Failed: {failure:,}</text>')

    parts.append('</svg>')
    return "\n".join(parts)


def _generate_mos_gauge_svg(mos: float, size: int = 180) -> str:
    """Generate a MOS quality gauge (1.0 - 5.0 scale)."""
    cx, cy = size // 2, size // 2 + 10
    r = size // 2 - 15
    mos = max(1.0, min(5.0, mos))

    # Map MOS 1-5 to 180 degrees (left to right)
    pct = (mos - 1.0) / 4.0
    angle_deg = 180 - pct * 180
    angle_rad = math.radians(angle_deg)

    # Colour based on MOS
    if mos >= 4.0:
        color = "#22c55e"
        label = "Excellent"
    elif mos >= 3.5:
        color = "#84cc16"
        label = "Good"
    elif mos >= 3.0:
        color = "#eab308"
        label = "Fair"
    elif mos >= 2.5:
        color = "#f97316"
        label = "Poor"
    else:
        color = "#ef4444"
        label = "Bad"

    parts: list[str] = []
    parts.append(f'<svg width="{size}" height="{size // 2 + 50}" xmlns="http://www.w3.org/2000/svg">')

    # Background arc
    parts.append(f'<path d="M{cx - r},{cy} A{r},{r} 0 0,1 {cx + r},{cy}" fill="none" stroke="#e5e7eb" stroke-width="14" stroke-linecap="round"/>')

    # Coloured arc
    needle_x = cx + r * math.cos(angle_rad)
    needle_y = cy - r * math.sin(angle_rad)
    large = 1 if pct > 0.5 else 0
    parts.append(f'<path d="M{cx - r},{cy} A{r},{r} 0 {large},1 {needle_x:.1f},{needle_y:.1f}" fill="none" stroke="{color}" stroke-width="14" stroke-linecap="round"/>')

    # Scale labels
    for val in (1, 2, 3, 4, 5):
        a = math.radians(180 - ((val - 1) / 4) * 180)
        tx = cx + (r + 14) * math.cos(a)
        ty = cy - (r + 14) * math.sin(a)
        parts.append(f'<text x="{tx:.0f}" y="{ty:.0f}" text-anchor="middle" font-size="9" fill="#666">{val}</text>')

    # Value
    parts.append(f'<text x="{cx}" y="{cy - 10}" text-anchor="middle" font-size="28" font-weight="bold" fill="{color}">{mos:.2f}</text>')
    parts.append(f'<text x="{cx}" y="{cy + 10}" text-anchor="middle" font-size="12" fill="#666">MOS</text>')
    parts.append(f'<text x="{cx}" y="{cy + 28}" text-anchor="middle" font-size="11" fill="{color}">{label}</text>')

    parts.append('</svg>')
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# HTML template fragments
# ---------------------------------------------------------------------------

_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f3f4f6; color: #1f2937; line-height: 1.5; padding: 20px; }
.container { max-width: 1100px; margin: 0 auto; }
.header { background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%); color: white; padding: 30px; border-radius: 8px; margin-bottom: 20px; }
.header h1 { font-size: 24px; margin-bottom: 4px; }
.header .subtitle { opacity: 0.85; font-size: 14px; }
.card { background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); padding: 24px; margin-bottom: 20px; }
.card h2 { font-size: 18px; margin-bottom: 16px; color: #374151; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; }
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; }
.kpi { text-align: center; padding: 16px; background: #f9fafb; border-radius: 6px; }
.kpi .value { font-size: 28px; font-weight: 700; color: #2563eb; }
.kpi .label { font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 2px; }
.charts { display: flex; flex-wrap: wrap; gap: 20px; align-items: flex-start; }
.chart-box { flex: 1; min-width: 250px; text-align: center; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
table th { background: #f3f4f6; padding: 8px 10px; text-align: left; font-weight: 600; cursor: pointer; user-select: none; border-bottom: 2px solid #e5e7eb; }
table th:hover { background: #e5e7eb; }
table td { padding: 8px 10px; border-bottom: 1px solid #f3f4f6; }
table tr:hover td { background: #f9fafb; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.badge-success { background: #dcfce7; color: #166534; }
.badge-fail { background: #fee2e2; color: #991b1b; }
.badge-warn { background: #fef3c7; color: #92400e; }
.badge-info { background: #dbeafe; color: #1e40af; }
.flow-diagram { font-family: 'Courier New', monospace; font-size: 12px; white-space: pre; overflow-x: auto; background: #f9fafb; padding: 16px; border-radius: 6px; line-height: 1.4; }
.alert-row { padding: 8px 12px; border-left: 4px solid #e5e7eb; margin-bottom: 6px; border-radius: 0 4px 4px 0; background: #f9fafb; }
.alert-row.critical { border-left-color: #ef4444; background: #fef2f2; }
.alert-row.warning { border-left-color: #f59e0b; background: #fffbeb; }
.alert-row.info { border-left-color: #3b82f6; background: #eff6ff; }
.footer { text-align: center; padding: 20px; color: #9ca3af; font-size: 12px; }
.rtp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; align-items: center; }
@media (max-width: 768px) { .charts { flex-direction: column; } .rtp-grid { grid-template-columns: 1fr; } }
"""

_JS = """
function sortTable(tableId, colIdx) {
    var table = document.getElementById(tableId);
    var tbody = table.querySelector('tbody');
    var rows = Array.from(tbody.querySelectorAll('tr'));
    var th = table.querySelectorAll('th')[colIdx];
    var asc = th.dataset.sort !== 'asc';
    th.dataset.sort = asc ? 'asc' : 'desc';
    rows.sort(function(a, b) {
        var va = a.cells[colIdx].textContent.trim();
        var vb = b.cells[colIdx].textContent.trim();
        var na = parseFloat(va), nb = parseFloat(vb);
        if (!isNaN(na) && !isNaN(nb)) return asc ? na - nb : nb - na;
        return asc ? va.localeCompare(vb) : vb.localeCompare(va);
    });
    rows.forEach(function(r) { tbody.appendChild(r); });
}
"""


# ---------------------------------------------------------------------------
# Report Generator
# ---------------------------------------------------------------------------

class ReportGenerator:
    """
    Generates standalone HTML reports from test data.

    Usage::

        data = ReportData(title="Load Test Results")
        data.summary = ReportTestSummary(...)
        data.cps_timeline = [CPSDataPoint(...), ...]

        gen = ReportGenerator()
        html_str = gen.generate(data)
        gen.save(data, "/tmp/report.html")
    """

    def generate(self, data: ReportData) -> str:
        """Generate the complete HTML report as a string."""
        sections: list[str] = []
        sections.append(self._render_header(data))
        sections.append(self._render_summary(data))
        sections.append(self._render_charts(data))

        if data.rtp_metrics:
            sections.append(self._render_rtp(data))
        if data.cdr_entries:
            sections.append(self._render_cdr_table(data))
        if data.sip_flows:
            sections.append(self._render_sip_flow(data))
        if data.alerts:
            sections.append(self._render_alerts(data))
        if data.notes:
            sections.append(self._render_notes(data))

        sections.append(self._render_footer(data))

        body = "\n".join(sections)

        page = (
            "<!DOCTYPE html>\n"
            '<html lang="en">\n<head>\n'
            '<meta charset="UTF-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            '<title>{title}</title>\n'
            '<style>\n{css}\n</style>\n'
            '</head>\n<body>\n'
            '<div class="container">\n{body}\n</div>\n'
            '<script>\n{js}\n</script>\n'
            '</body>\n</html>'
        ).format(
            title=html.escape(data.title),
            css=_CSS,
            body=body,
            js=_JS,
        )
        logger.info("Report generated: %s (%d bytes)", data.report_id, len(page))
        return page

    def save(self, data: ReportData, output_path: str) -> str:
        """Generate and write the report to a file.  Returns the path."""
        content = self.generate(data)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fp:
            fp.write(content)
        logger.info("Report saved: %s", output_path)
        return output_path

    # -- section renderers -------------------------------------------------

    @staticmethod
    def _render_header(data: ReportData) -> str:
        s = data.summary or ReportTestSummary()
        ts = data.summary.started_at.strftime("%Y-%m-%d %H:%M:%S UTC") if s.started_at else "N/A"
        return (
            '<div class="header">\n'
            '  <h1>{title}</h1>\n'
            '  <div class="subtitle">Report ID: {rid} | Generated: {now} | Test started: {ts}</div>\n'
            '</div>'
        ).format(
            title=html.escape(data.title),
            rid=html.escape(data.report_id),
            now=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            ts=ts,
        )

    @staticmethod
    def _render_summary(data: ReportData) -> str:
        s = data.summary or ReportTestSummary()
        rate = s.success_rate
        badge = "badge-success" if rate >= 99 else ("badge-warn" if rate >= 90 else "badge-fail")
        return (
            '<div class="card">\n'
            '  <h2>Test Summary</h2>\n'
            '  <div class="kpi-grid">\n'
            '    <div class="kpi"><div class="value">{total:,}</div><div class="label">Total Calls</div></div>\n'
            '    <div class="kpi"><div class="value">{success:,}</div><div class="label">Successful</div></div>\n'
            '    <div class="kpi"><div class="value">{failed:,}</div><div class="label">Failed</div></div>\n'
            '    <div class="kpi"><div class="value"><span class="{badge}">{rate:.1f}%</span></div><div class="label">Success Rate</div></div>\n'
            '    <div class="kpi"><div class="value">{max_cps:.1f}</div><div class="label">Peak CPS</div></div>\n'
            '    <div class="kpi"><div class="value">{avg_cps:.1f}</div><div class="label">Avg CPS</div></div>\n'
            '    <div class="kpi"><div class="value">{dur:.0f}s</div><div class="label">Duration</div></div>\n'
            '    <div class="kpi"><div class="value">{target}</div><div class="label">Target</div></div>\n'
            '  </div>\n'
            '</div>'
        ).format(
            total=s.total_calls,
            success=s.successful_calls,
            failed=s.failed_calls,
            rate=rate,
            badge=badge,
            max_cps=s.max_cps,
            avg_cps=s.avg_cps,
            dur=s.duration_seconds,
            target=html.escape(s.target or "N/A"),
        )

    @staticmethod
    def _render_charts(data: ReportData) -> str:
        s = data.summary or ReportTestSummary()
        cps_svg = _generate_cps_chart_svg(data.cps_timeline)
        pie_svg = _generate_pie_chart_svg(s.successful_calls, s.failed_calls)
        return (
            '<div class="card">\n'
            '  <h2>Performance Charts</h2>\n'
            '  <div class="charts">\n'
            '    <div class="chart-box">\n'
            '      <h3 style="font-size:14px;color:#666;margin-bottom:8px">CPS Over Time</h3>\n'
            '      {cps_svg}\n'
            '    </div>\n'
            '    <div class="chart-box" style="max-width:260px">\n'
            '      <h3 style="font-size:14px;color:#666;margin-bottom:8px">Success / Failure</h3>\n'
            '      {pie_svg}\n'
            '    </div>\n'
            '  </div>\n'
            '</div>'
        ).format(cps_svg=cps_svg, pie_svg=pie_svg)

    @staticmethod
    def _render_rtp(data: ReportData) -> str:
        m = data.rtp_metrics
        if m is None:
            return ""
        gauge_svg = _generate_mos_gauge_svg(m.mos)
        return (
            '<div class="card">\n'
            '  <h2>RTP Quality Metrics</h2>\n'
            '  <div class="rtp-grid">\n'
            '    <div>\n'
            '      <table>\n'
            '        <tr><td><strong>Codec</strong></td><td>{codec}</td></tr>\n'
            '        <tr><td><strong>Packets Sent</strong></td><td>{ps:,}</td></tr>\n'
            '        <tr><td><strong>Packets Received</strong></td><td>{pr:,}</td></tr>\n'
            '        <tr><td><strong>Packets Lost</strong></td><td>{pl:,} ({plp:.2f}%)</td></tr>\n'
            '        <tr><td><strong>Jitter</strong></td><td>{jit:.2f} ms</td></tr>\n'
            '        <tr><td><strong>Avg Latency</strong></td><td>{lat:.2f} ms</td></tr>\n'
            '        <tr><td><strong>R-Factor</strong></td><td>{rf:.1f}</td></tr>\n'
            '        <tr><td><strong>MOS</strong></td><td>{mos:.2f}</td></tr>\n'
            '      </table>\n'
            '    </div>\n'
            '    <div style="text-align:center">{gauge}</div>\n'
            '  </div>\n'
            '</div>'
        ).format(
            codec=html.escape(m.codec or "N/A"),
            ps=m.packets_sent, pr=m.packets_received,
            pl=m.packets_lost, plp=m.packet_loss_pct,
            jit=m.jitter_ms, lat=m.avg_latency_ms,
            rf=m.r_factor, mos=m.mos, gauge=gauge_svg,
        )

    @staticmethod
    def _render_cdr_table(data: ReportData) -> str:
        rows: list[str] = []
        for c in data.cdr_entries:
            badge_cls = "badge-success" if c.status in ("completed", "answered") else "badge-fail"
            rows.append(
                '<tr>'
                '<td>{cid}</td>'
                '<td>{caller}</td><td>{callee}</td>'
                '<td>{time}</td><td>{dur:.3f}</td>'
                '<td><span class="badge {bc}">{st}</span></td>'
                '<td>{code}</td><td>{codec}</td>'
                '<td>{mos:.2f}</td>'
                '</tr>'.format(
                    cid=html.escape(c.call_id[:12]),
                    caller=html.escape(c.caller),
                    callee=html.escape(c.callee),
                    time=html.escape(c.start_time),
                    dur=c.duration,
                    st=html.escape(c.status),
                    bc=badge_cls,
                    code=c.sip_code,
                    codec=html.escape(c.codec or ""),
                    mos=c.mos,
                )
            )
        thead = (
            '<tr>'
            '<th onclick="sortTable(\'cdr-table\',0)">Call ID</th>'
            '<th onclick="sortTable(\'cdr-table\',1)">Caller</th>'
            '<th onclick="sortTable(\'cdr-table\',2)">Callee</th>'
            '<th onclick="sortTable(\'cdr-table\',3)">Start Time</th>'
            '<th onclick="sortTable(\'cdr-table\',4)">Duration</th>'
            '<th onclick="sortTable(\'cdr-table\',5)">Status</th>'
            '<th onclick="sortTable(\'cdr-table\',6)">SIP Code</th>'
            '<th onclick="sortTable(\'cdr-table\',7)">Codec</th>'
            '<th onclick="sortTable(\'cdr-table\',8)">MOS</th>'
            '</tr>'
        )
        return (
            '<div class="card">\n'
            '  <h2>Call Detail Records ({count:,} entries)</h2>\n'
            '  <div style="overflow-x:auto">\n'
            '    <table id="cdr-table">\n'
            '      <thead>{thead}</thead>\n'
            '      <tbody>{rows}</tbody>\n'
            '    </table>\n'
            '  </div>\n'
            '</div>'
        ).format(count=len(data.cdr_entries), thead=thead, rows="\n".join(rows))

    @staticmethod
    def _render_sip_flow(data: ReportData) -> str:
        # Identify endpoints
        endpoints: list[str] = []
        seen: set[str] = set()
        for f in data.sip_flows:
            for ep in (f.source, f.destination):
                if ep not in seen:
                    seen.add(ep)
                    endpoints.append(ep)

        lines: list[str] = []
        col_w = 30

        # Header
        header = " " * 14
        for ep in endpoints:
            header += ep.center(col_w)
        lines.append(header)

        sep = " " * 14
        for _ in endpoints:
            sep += "|".center(col_w)
        lines.append(sep)

        for f in data.sip_flows:
            src_idx = endpoints.index(f.source) if f.source in endpoints else 0
            dst_idx = endpoints.index(f.destination) if f.destination in endpoints else len(endpoints) - 1

            row = list(" " * (col_w * len(endpoints)))
            for i in range(len(endpoints)):
                center = i * col_w + col_w // 2
                if center < len(row):
                    row[center] = "|"

            left = min(src_idx, dst_idx)
            right = max(src_idx, dst_idx)
            lp = left * col_w + col_w // 2
            rp = right * col_w + col_w // 2

            for i in range(lp + 1, rp):
                if i < len(row):
                    row[i] = "-"

            if src_idx < dst_idx and rp < len(row):
                row[rp] = ">"
            elif lp < len(row):
                row[lp] = "<"

            label = f.label[:22]
            mid = (lp + rp) // 2
            start = mid - len(label) // 2
            for i, ch in enumerate(label):
                pos = start + i
                if 0 <= pos < len(row):
                    row[pos] = ch

            ts = f.timestamp[:12] if f.timestamp else ""
            lines.append(f"{ts:<14}{''.join(row)}")
            lines.append(sep)

        diagram = html.escape("\n".join(lines))
        return (
            '<div class="card">\n'
            '  <h2>SIP Flow Diagram</h2>\n'
            '  <div class="flow-diagram">{diagram}</div>\n'
            '</div>'
        ).format(diagram=diagram)

    @staticmethod
    def _render_alerts(data: ReportData) -> str:
        items: list[str] = []
        for a in data.alerts:
            sev = a.severity.lower()
            items.append(
                '<div class="alert-row {sev}">'
                '<strong>[{ts}] {sev_up}</strong> - {name}: {msg}'
                '</div>'.format(
                    sev=sev,
                    ts=html.escape(a.timestamp),
                    sev_up=sev.upper(),
                    name=html.escape(a.rule_name),
                    msg=html.escape(a.message),
                )
            )
        return (
            '<div class="card">\n'
            '  <h2>Alert History ({count})</h2>\n'
            '  {items}\n'
            '</div>'
        ).format(count=len(data.alerts), items="\n".join(items))

    @staticmethod
    def _render_notes(data: ReportData) -> str:
        return (
            '<div class="card">\n'
            '  <h2>Notes</h2>\n'
            '  <p>{notes}</p>\n'
            '</div>'
        ).format(notes=html.escape(data.notes))

    @staticmethod
    def _render_footer(data: ReportData) -> str:
        return (
            '<div class="footer">\n'
            '  Generated by GenCall Report Generator | '
            '{now}\n'
            '</div>'
        ).format(now=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))

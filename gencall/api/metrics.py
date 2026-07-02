"""
Prometheus exposition endpoint (text format) — hand-rolled, dependency-free.

Everything exported here is already computed by the engines; this module only
renders it, so a scrape costs a few dict reads (plus one loop_stats row per
running campaign on the worker). GET /metrics is auth-gated with the same
X-API-Key as the rest of the API — point Prometheus at it with:

    scrape_configs:
      - job_name: gencall
        metrics_path: /metrics
        http_headers:
          X-API-Key:
            values: ["<key>"]
        static_configs:
          - targets: ["worker:8080"]

The worker router is wired in main.py (module singletons, same pattern as
routes.py / loops.py); the controller endpoint lives in controller/routes.py
and calls render_controller_metrics().
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from gencall import __version__
from gencall.api.routes import require_api_key

logger = logging.getLogger("gencall.metrics")

router = APIRouter(tags=["metrics"])

# Wired in main.py when the worker app is built; any of these may stay None
# (degraded mode) and the corresponding metrics are simply omitted.
stats_engine = None
loop_engine = None
loop_matcher = None
call_parser = None


def _escape(value) -> str:
    return (str(value).replace("\\", r"\\").replace('"', r"\"")
            .replace("\n", r"\n"))


class PromText:
    """Tiny builder for the Prometheus text exposition format."""

    def __init__(self):
        self._lines = []

    def header(self, name: str, mtype: str, help_text: str):
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {mtype}")

    def sample(self, name: str, value, labels: dict | None = None):
        if value is None:
            return
        if labels:
            inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
            self._lines.append(f"{name}{{{inner}}} {value}")
        else:
            self._lines.append(f"{name} {value}")

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


def render_worker_metrics(stats=None, loops=None, matcher=None, parser=None) -> str:
    """Render the worker's metrics; every section tolerates a missing source."""
    p = PromText()
    p.header("gencall_info", "gauge", "Build info (constant 1).")
    p.sample("gencall_info", 1, {"version": __version__, "role": "worker"})

    if stats is not None:
        snap = stats.get_current()
        p.header("gencall_active_instances", "gauge",
                 "Running SIPp instances (dialers + UAS).")
        p.sample("gencall_active_instances", snap.active_instances)
        p.header("gencall_calls_total", "counter",
                 "Calls attempted across running instances.")
        p.sample("gencall_calls_total", snap.total_calls)
        p.header("gencall_calls_successful_total", "counter",
                 "Calls completed successfully.")
        p.sample("gencall_calls_successful_total", snap.successful_calls)
        p.header("gencall_calls_failed_total", "counter", "Calls failed.")
        p.sample("gencall_calls_failed_total", snap.failed_calls)
        p.header("gencall_current_calls", "gauge", "Concurrent calls right now.")
        p.sample("gencall_current_calls", snap.current_calls)
        p.header("gencall_calls_per_second", "gauge", "Current attempt rate.")
        p.sample("gencall_calls_per_second", round(snap.calls_per_second, 3))
        p.header("gencall_avg_response_time_ms", "gauge",
                 "Average SIP response time.")
        p.sample("gencall_avg_response_time_ms", round(snap.avg_response_time_ms, 2))
        p.header("gencall_success_rate", "gauge", "Success rate, 0-100.")
        p.sample("gencall_success_rate", round(snap.success_rate, 2))

    if loops is not None:
        try:
            ans = loops.answer_status()
            p.header("gencall_uas_up", "gauge",
                     "1 when the persistent answer side (UAS) is running.")
            p.sample("gencall_uas_up", 1 if ans.get("running") else 0)
            p.header("gencall_uas_current_answered", "gauge",
                     "Calls the UAS is holding up right now.")
            p.sample("gencall_uas_current_answered", ans.get("current_answered"))
            p.header("gencall_uas_answered_total", "counter",
                     "Calls answered by the UAS since it started.")
            p.sample("gencall_uas_answered_total", ans.get("total_answered"))
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("metrics: answer_status failed: %s", e)

        try:
            campaigns = loops.list_campaigns()
            by_status: dict = {}
            for c in campaigns:
                by_status[c.get("status", "unknown")] = \
                    by_status.get(c.get("status", "unknown"), 0) + 1
            p.header("gencall_loop_campaigns", "gauge",
                     "Loop campaigns by lifecycle status.")
            for status, n in sorted(by_status.items()):
                p.sample("gencall_loop_campaigns", n, {"status": status})

            running = [c for c in campaigns if c.get("status") == "running"]
            if matcher is not None and running:
                p.header("gencall_loop_minutes_out_ms", "gauge",
                         "Answered outbound milliseconds (latest snapshot).")
                p.header("gencall_loop_minutes_in_ms", "gauge",
                         "Inbound milliseconds (latest snapshot).")
                p.header("gencall_loop_completion_pct", "gauge",
                         "Matched-in / answered-out, 0-100 (latest snapshot).")
                p.header("gencall_loop_calls_out", "gauge",
                         "Outbound calls in the campaign window.")
                p.header("gencall_loop_answered_out", "gauge",
                         "Outbound calls answered (2xx).")
                for c in running:
                    cid = c.get("id")
                    st = matcher.latest_stats(cid)
                    if not st:
                        continue
                    lbl = {"campaign": cid, "name": c.get("name") or ""}
                    p.sample("gencall_loop_minutes_out_ms", st.get("minutes_out_ms"), lbl)
                    p.sample("gencall_loop_minutes_in_ms", st.get("minutes_in_ms"), lbl)
                    p.sample("gencall_loop_completion_pct", st.get("completion_pct"), lbl)
                    p.sample("gencall_loop_calls_out", st.get("calls_out"), lbl)
                    p.sample("gencall_loop_answered_out", st.get("answered_out"), lbl)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("metrics: campaign metrics failed: %s", e)

    if parser is not None:
        p.header("gencall_parser_tracked_logs", "gauge",
                 "SIPp per-call log files the record parser is tailing.")
        p.sample("gencall_parser_tracked_logs", parser.tracked_count())

    return p.render()


def render_controller_metrics(aggregator=None) -> str:
    """Render the controller's fleet metrics from the aggregator's snapshots."""
    p = PromText()
    p.header("gencall_info", "gauge", "Build info (constant 1).")
    p.sample("gencall_info", 1, {"version": __version__, "role": "controller"})

    if aggregator is None:
        return p.render()

    statuses = aggregator.all_node_status()
    p.header("gencall_fleet_nodes", "gauge", "Enabled nodes known to the controller.")
    p.sample("gencall_fleet_nodes", len(statuses))
    p.header("gencall_fleet_nodes_online", "gauge", "Nodes currently online.")
    p.sample("gencall_fleet_nodes_online",
             sum(1 for s in statuses if s.get("online")))
    p.header("gencall_fleet_node_online", "gauge", "Per-node liveness (0/1).")
    for s in statuses:
        p.sample("gencall_fleet_node_online", 1 if s.get("online") else 0,
                 {"node_id": s.get("node_id")})

    fleet = aggregator.get_fleet_stats()
    agg = fleet.get("aggregate") or {}
    p.header("gencall_fleet_calls_per_second", "gauge",
             "Fleet-wide attempt rate (online nodes).")
    p.sample("gencall_fleet_calls_per_second", agg.get("calls_per_second"))
    p.header("gencall_fleet_current_calls", "gauge",
             "Fleet-wide concurrent calls (online nodes).")
    p.sample("gencall_fleet_current_calls", agg.get("current_calls"))
    p.header("gencall_fleet_calls_total", "counter",
             "Fleet-wide calls attempted (online nodes).")
    p.sample("gencall_fleet_calls_total", agg.get("total_calls"))
    p.header("gencall_fleet_success_rate", "gauge",
             "Fleet-wide success rate, 0-100 (online nodes).")
    p.sample("gencall_fleet_success_rate", agg.get("success_rate"))

    return p.render()


@router.get("/metrics", dependencies=[Depends(require_api_key)],
            include_in_schema=False, response_class=PlainTextResponse)
def worker_metrics():
    return PlainTextResponse(
        render_worker_metrics(stats=stats_engine, loops=loop_engine,
                              matcher=loop_matcher, parser=call_parser),
        media_type="text/plain; version=0.0.4",
    )

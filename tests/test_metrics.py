"""
Prometheus /metrics rendering (worker + controller).

Pure rendering tests — no app boot, no scrape client. The endpoint itself is
just PlainTextResponse(render_...()) behind the standard auth dependency, which
the auth suite already covers.
"""

from gencall.api.metrics import (
    PromText, render_controller_metrics, render_worker_metrics,
)
from gencall.core.stats import StatsSnapshot


class FakeStats:
    def get_current(self):
        return StatsSnapshot(
            timestamp=123.0, active_instances=2, total_calls=100,
            successful_calls=90, failed_calls=10, current_calls=7,
            calls_per_second=1.5, avg_response_time_ms=42.0, success_rate=90.0,
        )


class FakeLoops:
    def answer_status(self):
        return {"running": True, "state": "running", "current_answered": 3,
                "max_answered": 50, "total_answered": 250}

    def list_campaigns(self):
        return [
            {"id": "loop-1", "name": "algeria", "status": "running"},
            {"id": "loop-2", "name": "done", "status": "stopped"},
        ]


class FakeMatcher:
    def latest_stats(self, campaign_id):
        if campaign_id != "loop-1":
            return None
        return {"minutes_out_ms": 60000, "minutes_in_ms": 55000,
                "completion_pct": 91.7, "calls_out": 10, "answered_out": 9}


class FakeParser:
    def tracked_count(self):
        return 4


class FakeAggregator:
    def all_node_status(self):
        return [
            {"node_id": 1, "online": True},
            {"node_id": 2, "online": False},
        ]

    def get_fleet_stats(self):
        return {"aggregate": {"calls_per_second": 3.0, "current_calls": 12,
                              "total_calls": 500, "success_rate": 88.0},
                "per_group": {}, "per_node": {}}


def test_promtext_escapes_label_values():
    p = PromText()
    p.header("m", "gauge", "h")
    p.sample("m", 1, {"name": 'a"b\\c\nd'})
    out = p.render()
    assert 'name="a\\"b\\\\c\\nd"' in out


def test_promtext_skips_none_samples():
    p = PromText()
    p.sample("m", None)
    assert p.render() == "\n"


def test_worker_metrics_tolerate_missing_sources():
    out = render_worker_metrics()
    assert 'gencall_info{version=' in out
    assert 'role="worker"' in out
    # No engines wired -> no engine metrics, but still valid exposition text.
    assert "gencall_calls_total" not in out


def test_worker_metrics_full():
    out = render_worker_metrics(stats=FakeStats(), loops=FakeLoops(),
                                matcher=FakeMatcher(), parser=FakeParser())
    assert "gencall_calls_total 100" in out
    assert "gencall_current_calls 7" in out
    assert "gencall_uas_up 1" in out
    assert "gencall_uas_answered_total 250" in out
    assert 'gencall_loop_campaigns{status="running"} 1' in out
    assert 'gencall_loop_campaigns{status="stopped"} 1' in out
    # Per-campaign gauges only for running campaigns with a stats snapshot.
    assert ('gencall_loop_minutes_out_ms{campaign="loop-1",name="algeria"} 60000'
            in out)
    assert "loop-2" not in out.replace('status="stopped"', "")
    assert "gencall_parser_tracked_logs 4" in out
    # Every sample line's metric name was declared with HELP/TYPE.
    declared = {ln.split()[2] for ln in out.splitlines() if ln.startswith("# TYPE")}
    for ln in out.splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        name = ln.split("{")[0].split()[0]
        assert name in declared, f"undeclared metric {name}"


def test_controller_metrics():
    out = render_controller_metrics(aggregator=FakeAggregator())
    assert 'role="controller"' in out
    assert "gencall_fleet_nodes 2" in out
    assert "gencall_fleet_nodes_online 1" in out
    assert 'gencall_fleet_node_online{node_id="1"} 1' in out
    assert 'gencall_fleet_node_online{node_id="2"} 0' in out
    assert "gencall_fleet_calls_per_second 3.0" in out


def test_controller_metrics_without_aggregator():
    out = render_controller_metrics(aggregator=None)
    assert 'role="controller"' in out
    assert "gencall_fleet_nodes" not in out


def test_grafana_dashboard_references_only_exported_metrics():
    """Guard: every gencall_* metric the shipped dashboard queries must be one
    the code actually exports, so a metric rename can't silently break panels."""
    import json
    import os
    import re

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dash = os.path.join(repo, "deploy", "grafana-gencall.json")
    raw = open(dash, encoding="utf-8").read()
    json.loads(raw)  # must be valid JSON

    referenced = set(re.findall(r"gencall_[a-z_]+", raw))

    worker = render_worker_metrics(stats=FakeStats(), loops=FakeLoops(),
                                   matcher=FakeMatcher(), parser=FakeParser())
    controller = render_controller_metrics(aggregator=FakeAggregator())
    exported = set(re.findall(r"gencall_[a-z_]+",
                              worker + "\n" + controller))

    missing = referenced - exported
    assert not missing, f"dashboard references unexported metrics: {sorted(missing)}"

"""
AlertNotifier (operational webhook alerts).

Delivery is exercised without any network: _send is monkeypatched (or the
queue is inspected directly). Covers event filtering, the per event+key
throttle, the completion check's guards, and the liveness-transition listener.
"""

import time

from gencall.core.alerts import COMPLETION_MIN_ANSWERED, AlertNotifier, build_from_config


def drain(notifier):
    """Pop every queued payload without running the sender thread."""
    out = []
    while not notifier._queue.empty():
        out.append(notifier._queue.get_nowait())
    return out


def test_notify_queues_payload_with_source_and_data():
    n = AlertNotifier(url="http://alerts.example/hook", source="worker@box1",
                      min_interval_s=0)
    assert n.notify("uas_restarted", {"previous_state": "error"}, key="uas")
    (p,) = drain(n)
    assert p["event"] == "uas_restarted"
    assert p["source"] == "worker@box1"
    assert p["data"] == {"previous_state": "error"}
    assert p["timestamp"] <= time.time()


def test_notify_disabled_without_url_and_filtered_by_allowlist():
    assert not AlertNotifier(url="").notify("uas_restarted")
    n = AlertNotifier(url="http://alerts.example/hook",
                      events=["node_offline"], min_interval_s=0)
    assert not n.notify("uas_restarted")
    assert n.notify("node_offline")
    assert len(drain(n)) == 1


def test_throttle_per_event_and_key():
    n = AlertNotifier(url="http://alerts.example/hook", min_interval_s=3600)
    assert n.notify("node_offline", key="1")
    # Same event+key inside the window: suppressed.
    assert not n.notify("node_offline", key="1")
    # Different key or different event: allowed.
    assert n.notify("node_offline", key="2")
    assert n.notify("node_online", key="1")
    assert len(drain(n)) == 3


def test_check_completion_guards_and_fire():
    n = AlertNotifier(url="http://alerts.example/hook",
                      completion_min_pct=80.0, min_interval_s=0)
    # Disabled threshold -> never fires.
    off = AlertNotifier(url="http://alerts.example/hook", completion_min_pct=0)
    off.check_completion({"campaign_id": "c", "completion_pct": 1,
                          "answered_out": 100})
    assert drain(off) == []
    # Too small a sample -> no alert even at 0 %.
    n.check_completion({"campaign_id": "c1", "completion_pct": 0.0,
                        "answered_out": COMPLETION_MIN_ANSWERED - 1})
    assert drain(n) == []
    # Healthy completion -> no alert.
    n.check_completion({"campaign_id": "c1", "completion_pct": 92.0,
                        "answered_out": 50})
    assert drain(n) == []
    # Below threshold with enough sample -> alert with the numbers.
    n.check_completion({"campaign_id": "c1", "completion_pct": 40.0,
                        "answered_out": 50})
    (p,) = drain(n)
    assert p["event"] == "loop_completion_low"
    assert p["data"]["completion_pct"] == 40.0
    assert p["data"]["threshold_pct"] == 80.0


def test_node_status_listener_fires_on_transitions_only():
    n = AlertNotifier(url="http://alerts.example/hook", min_interval_s=0)
    listener = n.make_node_status_listener()
    # First sighting establishes state without alerting.
    listener({"node_id": 1, "online": True})
    # Non-liveness change (aggregator notifies on any change) -> no alert.
    listener({"node_id": 1, "online": True, "active_tests": 3})
    assert drain(n) == []
    # Flip offline -> alert; flip back -> alert.
    listener({"node_id": 1, "online": False, "error": "boom"})
    listener({"node_id": 1, "online": True})
    events = [p["event"] for p in drain(n)]
    assert events == ["node_offline", "node_online"]


def test_sender_thread_delivers_via_send(monkeypatch):
    n = AlertNotifier(url="http://alerts.example/hook", min_interval_s=0)
    sent = []
    monkeypatch.setattr(n, "_send", lambda payload: sent.append(payload) or True)
    n.start()
    try:
        n.notify("uas_restarted", key="uas")
        deadline = time.time() + 5
        while not sent and time.time() < deadline:
            time.sleep(0.05)
    finally:
        n.stop()
    assert sent and sent[0]["event"] == "uas_restarted"


def test_build_from_config(tmp_path):
    from gencall.core.config import Config
    cfg_file = tmp_path / "gencall.cfg"
    cfg_file.write_text(
        "[alerts]\n"
        "webhook_url = http://alerts.example/hook\n"
        "webhook_secret = s3cret\n"
        "events = node_offline, uas_restarted\n"
        "min_interval_s = 5\n"
        "completion_min_pct = 75\n"
    )
    Config.reset()
    try:
        cfg = Config(str(cfg_file))
        n = build_from_config(cfg, source="test@here")
        assert n is not None
        assert n.callback.url == "http://alerts.example/hook"
        assert n.callback.secret == "s3cret"
        assert n.events == {"node_offline", "uas_restarted"}
        assert n.min_interval_s == 5.0
        assert n.completion_min_pct == 75.0

        # Unconfigured -> None.
        Config.reset()
        empty = tmp_path / "empty.cfg"
        empty.write_text("[web]\nport = 8080\n")
        assert build_from_config(Config(str(empty))) is None
    finally:
        Config.reset()

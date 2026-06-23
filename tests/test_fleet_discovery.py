"""
Tests for VLAN fleet discovery (gencall/core/discovery.py) + headless config.

Pure beacon encode/parse, the controller-side node upsert, the listener thread
(over a unicast loopback packet so no broadcast/timing flakiness), and the
[web] serve_console / --headless toggle.
"""

import os
import socket
import tempfile
import time

import pytest

from gencall.core import discovery as d


# ─── pure wire format ─────────────────────────────────────────────────────────

def test_beacon_roundtrip():
    b = d.build_beacon("tok", "http://10.0.0.5:8080", hostname="vd1", version="2.0")
    info = d.parse_beacon(d.encode_beacon(b), "tok")
    assert info == {"address": "http://10.0.0.5:8080", "hostname": "vd1", "version": "2.0"}


def test_parse_rejects_wrong_token():
    raw = d.encode_beacon(d.build_beacon("secret", "http://10.0.0.5:8080"))
    assert d.parse_beacon(raw, "different") is None
    assert d.parse_beacon(raw, "secret") is not None


def test_parse_empty_token_rejects_all():
    # Fail closed (v2.2.6): with no fleet token configured, every beacon is
    # rejected — discovery must not auto-register foreign/forged nodes.
    raw = d.encode_beacon(d.build_beacon("whatever", "http://10.0.0.9:8080"))
    assert d.parse_beacon(raw, "") is None
    # a matching token still parses normally
    raw2 = d.encode_beacon(d.build_beacon("tok", "http://10.0.0.9:8080"))
    assert d.parse_beacon(raw2, "tok") is not None


def test_parse_rejects_garbage_and_foreign():
    assert d.parse_beacon(b"not json", "") is None
    assert d.parse_beacon(b'{"hello":1}', "") is None  # no magic
    assert d.parse_beacon(d.encode_beacon({"magic": d.BEACON_MAGIC, "token": ""}), "") is None  # no address
    assert d.parse_beacon(b"x" * (d.MAX_BEACON_BYTES + 1), "") is None  # oversized


# ─── controller-side upsert ───────────────────────────────────────────────────

@pytest.fixture
def ctrl_db():
    from gencall.controller.models import ControllerDatabase
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = ControllerDatabase("sqlite:///" + tmp.name)
    db.create_tables()
    return db


def test_upsert_creates_then_updates_idempotently(ctrl_db):
    from gencall.controller.models import Node

    info = {"address": "http://10.0.0.5:8080/", "hostname": "vd1", "version": "2.0"}
    assert d.upsert_discovered_node(ctrl_db, info, "tok") == "created"
    # Re-announce: same address → updated, NOT a duplicate row.
    assert d.upsert_discovered_node(ctrl_db, info, "tok") == "updated"

    session = ctrl_db.get_session()
    try:
        nodes = session.query(Node).filter_by(address="http://10.0.0.5:8080").all()
        assert len(nodes) == 1
        assert nodes[0].api_key == "tok"  # shared token becomes the command key
        assert nodes[0].enabled
    finally:
        session.close()


# ─── listener thread (unicast loopback, no broadcast) ─────────────────────────

def test_listener_receives_and_dispatches():
    port = 45917
    got = []
    listener = d.BeaconListener(lambda info: got.append(info), port=port, token="tok")
    listener.start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        payload = d.encode_beacon(d.build_beacon("tok", "http://127.0.0.1:8080", hostname="h"))
        deadline = time.time() + 3
        while not got and time.time() < deadline:
            s.sendto(payload, ("127.0.0.1", port))
            time.sleep(0.1)
        s.close()
        assert got and got[0]["address"] == "http://127.0.0.1:8080"
    finally:
        listener.stop()


def test_listener_ignores_foreign_token():
    port = 45918
    got = []
    listener = d.BeaconListener(lambda info: got.append(info), port=port, token="right")
    listener.start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        payload = d.encode_beacon(d.build_beacon("wrong", "http://127.0.0.1:8080"))
        for _ in range(5):
            s.sendto(payload, ("127.0.0.1", port))
            time.sleep(0.05)
        s.close()
        time.sleep(0.2)
        assert got == []  # foreign-token beacon never dispatched
    finally:
        listener.stop()


# ─── headless toggle ──────────────────────────────────────────────────────────

def test_serve_console_default_and_headless_env(monkeypatch):
    from gencall.core.config import Config

    Config.reset()
    monkeypatch.delenv("GENCALL_HEADLESS", raising=False)
    assert Config().serve_console is True  # default: serve the console

    monkeypatch.setenv("GENCALL_HEADLESS", "1")
    assert Config().serve_console is False  # --headless / env forces lean worker
    Config.reset()

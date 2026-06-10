"""
Packaging self-check (design §4.1, §4.5, §5, §7 stage 10).

This host (Windows) cannot build/run Docker or Linux, so the Docker/compose
deliverables are validated by **presence + a parse/lint check only** — never by
building an image. These tests assert the structural promises the deploy depends
on, so a regression in the packaging files is caught in CI without a daemon:

  * the worker Dockerfile builds SIPp from source with USE_SCTP=0 and installs
    it to /usr/local/bin/sipp;
  * docker-compose.v2.yml exists, parses as YAML, uses host networking for the
    worker, keeps Postgres on loopback, and exposes a configurable RTP range;
  * docs/deploy/loop-runner.md ships BOTH an nftables and a ufw rule set scoped
    to UDP/5060 + the RTP range.

No real SIPp/Docker/Linux is touched.
"""

import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    path = os.path.join(REPO_ROOT, rel)
    assert os.path.isfile(path), f"missing deliverable: {rel}"
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ── Dockerfile (worker) ──────────────────────────────────────────────────────


def test_dockerfile_builds_sipp_from_source_no_sctp():
    df = _read("gencall/Dockerfile")
    # Built from source (git clone of upstream SIPp), not a package install.
    assert "github.com/SIPp/sipp.git" in df
    # SCTP explicitly OFF (EPEL has no UBI9 sipp; SCTP headers unreachable).
    assert "-DUSE_SCTP=0" in df
    # Installed to the canonical path that gencall.cfg [sipp] command defaults to.
    assert "/usr/local/bin/sipp" in df
    # Media (pcap play) + TLS transport kept on.
    assert "-DUSE_PCAP=1" in df and "-DUSE_SSL=1" in df


def test_dockerfile_keeps_image_lean():
    df = _read("gencall/Dockerfile")
    # The build toolchain is removed in the same layer so it never ships.
    assert "dnf remove" in df
    # A version pin keeps the build reproducible.
    assert "SIPP_VERSION" in df


# ── docker-compose.v2.yml ────────────────────────────────────────────────────


def test_compose_v2_parses_and_uses_host_networking():
    yaml = pytest.importorskip("yaml")
    raw = _read("docker-compose.v2.yml")
    doc = yaml.safe_load(raw)

    services = doc.get("services", {})
    assert {"gencall", "controller", "postgres"} <= set(services), \
        "v2 compose must define worker + controller + postgres"

    worker = services["gencall"]
    # Host networking for the worker — avoids the docker-proxy RTP memory blowup.
    assert worker.get("network_mode") == "host"
    # With host networking there is no published-port list on the worker.
    assert "ports" not in worker, \
        "host-networked worker must not publish ports (no docker-proxy)"

    # Configurable RTP range surfaced for the firewall / gencall.cfg sync.
    env = worker.get("environment", [])
    env_text = "\n".join(env) if isinstance(env, list) else str(env)
    assert "RTP_PORT_RANGE" in env_text

    # Postgres bound to loopback only — never exposed off-box.
    pg_ports = services["postgres"].get("ports", [])
    assert any("127.0.0.1:5432" in str(p) for p in pg_ports), \
        "Postgres must publish only on 127.0.0.1"


def test_compose_v2_does_not_replace_v1():
    # The v1 compose (bridged/multi-box fleet) is kept intact alongside v2.
    assert os.path.isfile(os.path.join(REPO_ROOT, "docker-compose.yml"))
    assert os.path.isfile(os.path.join(REPO_ROOT, "docker-compose.v2.yml"))


# ── deploy doc (firewall = real trust boundary, §4.1) ────────────────────────


def test_deploy_doc_ships_nftables_and_ufw_rules():
    doc = _read("docs/deploy/loop-runner.md")
    # Both rule sets present.
    assert "nftables" in doc and "ufw" in doc
    # Both scope SIP signalling and the RTP range to the whitelist.
    assert "5060" in doc
    assert "RTP_LO" in doc and "RTP_HI" in doc
    # Default-deny is the posture.
    assert "policy drop" in doc            # nftables
    assert "default deny incoming" in doc  # ufw
    # The whitelist / trust boundary is named.
    assert "MADA" in doc and "whitelist" in doc.lower()

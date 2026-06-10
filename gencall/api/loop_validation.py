"""
Loop-campaign input validation (security hardening).

Two classes of check live here, both applied before a Loop Campaign is ever
spawned:

  * ``validate_dest_host`` — the open SIP-originator / SSRF guard. ``dest_host``
    comes straight off the wire and flows to the SIPp ``-rsa`` target, so an
    unvalidated value lets a caller turn the box into an internal-network SIP
    originator/port-scanner. We reject private/loopback/multicast/link-local and
    the unspecified address (0.0.0.0 / ::) unless an explicit config allow-list
    permits the exact IP or its CIDR.

  * ``validate_caps`` — the OOM guard. Rate and per-campaign channel counts are
    bounded against config caps so a single start can't request more than the
    4 GB box can serve.

Both raise ``ValueError`` with a human-readable message; the API layer maps that
to HTTP 422.
"""

import ipaddress
import socket


# Transports SIPp actually supports here. An unknown transport must be a hard
# 422, not a silent downgrade to UDP (which would mask a client bug / send
# cleartext when TLS was intended).
ALLOWED_TRANSPORTS = ("udp", "tcp", "tls")


class DestHostError(ValueError):
    """Raised when a dest_host is not an allowed destination."""


def _ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    """True when an IP is in a range we refuse to originate toward by default."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_multicast
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
    )


def _allowed_by_list(ip: ipaddress._BaseAddress, allowlist) -> bool:
    """True when ``ip`` is explicitly permitted by a config allow-list entry.

    Each entry may be a bare IP or a CIDR. A malformed entry is skipped (it can
    never widen access).
    """
    for entry in allowlist or ():
        entry = (entry or "").strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            elif ip == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def validate_dest_host(dest_host: str, allowlist=None) -> str:
    """Return ``dest_host`` unchanged if allowed, else raise ``DestHostError``.

    Accepts either an IP literal or a hostname. A hostname is resolved and EVERY
    resolved address must pass the same block check (so a name pointing at
    127.0.0.1 / an RFC1918 address is refused just like the literal). The config
    ``allowlist`` (exact IPs or CIDRs) is the only way to permit an otherwise
    blocked address.
    """
    host = (dest_host or "").strip()
    if not host:
        raise DestHostError("dest_host is required")

    # Literal IP path.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if _allowed_by_list(ip, allowlist):
            return host
        if _ip_blocked(ip):
            raise DestHostError(
                f"dest_host {host!r} is a private/loopback/multicast/reserved "
                "address; not allowed as a loop destination (add it to "
                "[loops] dest_allowlist to permit)"
            )
        return host

    # Hostname path: resolve and check every A/AAAA record.
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise DestHostError(f"dest_host {host!r} could not be resolved: {exc}")

    resolved = {info[4][0] for info in infos}
    for addr in resolved:
        try:
            rip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _allowed_by_list(rip, allowlist):
            continue
        if _ip_blocked(rip):
            raise DestHostError(
                f"dest_host {host!r} resolves to a private/loopback/multicast/"
                f"reserved address ({addr}); not allowed as a loop destination"
            )
    return host


def validate_transport(transport: str) -> str:
    """Return a normalized transport or raise ``ValueError`` for an unknown one.

    Rejecting (rather than silently downgrading to UDP) keeps a TLS-intended
    campaign from going out in cleartext when a typo'd transport slips through.
    """
    t = (transport or "").strip().lower()
    if t not in ALLOWED_TRANSPORTS:
        raise ValueError(
            f"transport {transport!r} is not one of {ALLOWED_TRANSPORTS}"
        )
    return t


def validate_caps(rate: float, max_concurrent: int, config) -> None:
    """Bound rate + per-campaign channels against config caps. Raises ValueError.

    Negatives/zero are already rejected by the pydantic model; this enforces the
    upper bounds (which depend on runtime config and so cannot live on the model).
    """
    max_rate = config.loops_max_rate_cps
    if rate > max_rate:
        raise ValueError(
            f"rate {rate} exceeds the per-campaign cap of {max_rate} cps"
        )
    max_channels = config.loops_max_channels
    if max_concurrent > max_channels:
        raise ValueError(
            f"max_concurrent {max_concurrent} exceeds the per-campaign "
            f"channel cap of {max_channels}"
        )
